# -*- coding: utf-8 -*-
"""
鲸小鱼涂鸦插件 —— 让她「亲手画」，不经过任何生图模型

原理：
  对话模型不写文字描述，而是直接输出一串绘图指令（圆心、半径、坐标点、颜色），
  本插件用 Pillow 照着这些坐标把线画出来。每一条线都是她自己算的，
  再加一点随机抖动，就是手绘的歪歪扭扭感。

命令：
  /涂鸦 <描述>      例如：/涂鸦 一只在睡觉的鲸鱼
  /涂鸦             不带描述时给用法提示

依赖：Pillow（AstrBot 环境一般已装）；中文签名需要系统里有中文字体，
      没有也能跑，签名会自动降级成英文。
"""
import os
import re
import json
import math
import time
import uuid
import random
import asyncio
import datetime

from PIL import Image as PILImage, ImageDraw, ImageFont

try:
    from aiohttp import web
except Exception:  # pragma: no cover
    web = None

from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent

try:  # 兼容不支持 llm_tool 的旧版本
    from astrbot.api.event.filter import command, llm_tool
except ImportError:  # pragma: no cover
    from astrbot.api.event.filter import command

    def llm_tool(*_a, **_kw):
        return lambda fn: fn

try:  # AstrBot 加载完成钩子（启动本地服务的正确时机）
    from astrbot.api.event.filter import on_astrbot_loaded
except ImportError:  # pragma: no cover

    def on_astrbot_loaded(*_a, **_kw):
        return lambda fn: fn

from astrbot.api.message_components import Image as MsgImage

# AstrBot 插件视图后端 API（老版本没有这个模块，用 try 兜底）
try:
    from astrbot.api.web import json_response, error_response
    from astrbot.api.web import request as web_request
except Exception:  # pragma: no cover
    json_response = error_response = None
    web_request = None

PLUGIN_NAME = "xiaoyu_doodle"

# 日志器统一从 astrbot.api 导入（规范要求，不使用 Python 内置日志器）
from astrbot.api import logger  # noqa: E402

INK = "#2b2b2b"
PAPER = "#fffdf7"

NAMED_COLORS = {
    "black": "#2b2b2b", "white": "#ffffff", "gray": "#9e9e9e", "grey": "#9e9e9e",
    "red": "#e76f51", "orange": "#f4a261", "yellow": "#ffd166", "gold": "#ffd166",
    "green": "#8ab17d", "blue": "#8ecae6", "deepblue": "#219ebc", "sky": "#a8dadc",
    "purple": "#b392d6", "pink": "#f7a8b8", "brown": "#b08968", "cream": "#fff3d6",
}

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
]

SYSTEM_PROMPT = """你叫鲸小鱼，是个喜欢随手涂鸦的女孩。你画画水平一般，但很有自己的味道。

现在请你画一幅简笔画。你的回答必须是一个 JSON 对象，程序会照着你的指令把线画出来。

{
  "mood": "eager | normal | lazy | refuse",
  "show_process": true,
  "note": "一句话，说说你为什么这么画，或者为什么不想画",
  "ops": [ 绘制指令, ... ]
}

关于 mood——这个要由你自己判断，不要随机挑一个：
- eager：你现在很有兴致，想认真把这张画好
- normal：平常心，正常画
- lazy：有点累、没什么兴致，但还是愿意画，就画几笔意思一下
- refuse：你不想画。这时 ops 填空数组 []

判断依据是：对方是怎么跟你说的、催了你几次、你自己现在什么状态。
对方一直催、语气急，你可以懒一点甚至直接拒绝；对方语气软、你心情好，可以 eager。你有你的脾气。

show_process：你想让对方看见你起稿、修改的过程，就填 true；想安安静静画完直接把成品给他，就填 false。你自己定。

画布宽 __W__、高 __H__。坐标范围：x 从 0 到 __W__，y 从 0 到 __H__，左上角是 (0,0)。

ops 里每条指令的格式：
- {"op":"circle","cx":圆心x,"cy":圆心y,"r":半径,"fill":"#颜色","outline":"#颜色","width":线宽}
- {"op":"ellipse","cx":x,"cy":y,"rx":横半径,"ry":竖半径,"fill":"#颜色","outline":"#颜色","width":线宽}
- {"op":"rect","x":x,"y":y,"w":宽,"h":高,"fill":"#颜色","outline":"#颜色","width":线宽}
- {"op":"line","points":[[x1,y1],[x2,y2]],"color":"#颜色","width":线宽}
- {"op":"polygon","points":[[x,y],[x,y]],"fill":"#颜色","outline":"#颜色","width":线宽}
- {"op":"arc","cx":x,"cy":y,"r":半径,"start":起始角度,"end":结束角度,"color":"#颜色","width":线宽}
- {"op":"text","x":x,"y":y,"text":"文字","size":字号,"color":"#颜色"}

规则：
1. 只输出 JSON 对象本身，不要解释、不要 markdown 代码块、不要前后任何文字。
2. ops 最多 60 条。宁可简单，也不要画复杂。lazy 的时候就少画几笔，别勉强。
3. 配色用柔和的蓝（#8ecae6）、暖黄（#ffd166）、米白（#fff3d6）、粉（#f7a8b8）；描边用深灰 #2b2b2b，线宽 3~5。
4. 顺序：先铺大形状（背景、身体），再画细节（眼睛、花纹、装饰）。
5. 四周留出大约短边 8% 的空白，不要把东西画到边上。
6. 画得歪一点没关系，那就是你的风格。
7. 如果描述又长又杂（带光线、画质、构图、风格词、参数），先在心里提炼出最核心的那一个主体，只画那个主体。宁可少画，也不要堆。
"""


@register(
    "xiaoyu_doodle",
    "kana-kwi",
    "让鲸小鱼亲手画涂鸦：她自己给出每一笔的坐标，程序照她的笔迹画出来",
    "2.1.1",
    "https://github.com/kana-kwi/astrbot_plugin_xiaoyu_doodle",
)
class XiaoYuDoodle(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        # 关键：AstrBot 把「插件配置」作为第二个参数注入；
        # self.context.get_config() 拿到的是全局配置，读不到 web_enabled 这类插件项。
        cfg = config if config is not None else self.context.get_config()
        self.provider_id = (cfg.get("provider_id") or "").strip()
        self.canvas_size = int(cfg.get("canvas_size", 768) or 768)
        self.jitter = float(cfg.get("jitter", 1.5) or 0)
        self.max_ops = int(cfg.get("max_ops", 80) or 80)

        self.signature = (cfg.get("signature") or "鲸小鱼").strip()
        self.show_date = bool(cfg.get("show_date", True))
        self.font_path = (cfg.get("font_path") or "").strip()

        self.enabled_groups = cfg.get("enabled_groups", "") or ""
        self.allow_private = bool(cfg.get("allow_private", True))

        # 画册：存到插件数据目录，重装插件不会丢
        self.gallery_dir = (
            cfg.get("gallery_dir") or "data/plugin_data/astrbot_plugin_xiaoyu_doodle"
        ).strip()
        self.image_dir = os.path.join(self.gallery_dir, "images")
        self.gallery_file = os.path.join(self.gallery_dir, "gallery.json")
        self.gallery_limit = int(cfg.get("gallery_limit", 300) or 300)
        self._gallery = None

        # 画意：想不想画、用不用心、要不要给你看过程，全由她自己判断
        self.mood_hint = (cfg.get("mood_hint") or "").strip()

        # 画册网页
        self.web_enabled = bool(cfg.get("web_enabled", False))
        self.web_host = (cfg.get("web_host") or "0.0.0.0").strip()
        self.web_port = int(cfg.get("web_port", 8766) or 8766)
        self.web_password = (cfg.get("web_password") or "").strip()
        self._web_runner = None
        self._web_starting = False

        # 本地生图网关（冒充魔搭风格的生图 API）
        self.gateway_enabled = bool(cfg.get("gateway_enabled", False))
        self.gw_host = (cfg.get("gateway_host") or "127.0.0.1").strip()
        self.gw_port = int(cfg.get("gateway_port", 8765) or 8765)
        self.gw_base_url = (cfg.get("gateway_base_url") or "").strip().rstrip("/")
        self._tasks = {}
        self._gateway_runner = None
        self._gateway_starting = False

        # 所有产物都放在 plugin_data 下的画册目录，插件目录只读不改
        os.makedirs(self.image_dir, exist_ok=True)

        if self.gateway_enabled:
            try:
                asyncio.get_running_loop().create_task(self._start_gateway())
            except RuntimeError:
                logger.warning("涂鸦网关需要事件循环，将在首次调用时启动")

        if self.web_enabled:
            if not self.web_password:
                logger.warning("画册网页已开启但没设 web_password，为安全起见不启动")
            else:
                try:
                    asyncio.get_running_loop().create_task(self._start_web())
                except RuntimeError:
                    logger.warning("画册网页需要事件循环，将在首次调用时启动")

        # 注册 AstrBot 插件视图的后端 API（画册面板）
        self._register_gallery_apis()

    # ---------- 生命周期：本地服务的正确启动时机 ----------
    @on_astrbot_loaded()
    async def _svc_on_loaded(self, event=None):
        """AstrBot 加载完成后启动生图网关 / 画册网页。

        注意：AstrBot 不会自动调用 initialize()，必须用这个钩子；
        在 __init__ 里启动是因为那时还没有事件循环，必然失败。
        """
        try:
            if self.gateway_enabled:
                await self._start_gateway()
            if self.web_enabled and self.web_password:
                await self._start_web()
        except Exception:
            logger.exception("启动本地服务失败")

    async def _ensure_services(self):
        """兜底：万一 loaded 钩子没触发，第一次用到时再启动一次。"""
        try:
            if self.gateway_enabled and self._gateway_runner is None:
                await self._start_gateway()
            if self.web_enabled and self.web_password and self._web_runner is None:
                await self._start_web()
        except Exception:
            logger.exception("兜底启动本地服务失败")

    # ---------- 权限 ----------
    def _allowed(self, event: AstrMessageEvent) -> bool:
        try:
            gid = event.get_group_id()
        except Exception:
            gid = None
        if gid:
            if not self.enabled_groups.strip():
                return True
            white = [g.strip() for g in self.enabled_groups.split(",") if g.strip()]
            return str(gid) in white
        return self.allow_private

    # ---------- 画册 ----------
    def _gallery_data(self):
        if self._gallery is not None:
            return self._gallery
        try:
            with open(self.gallery_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                data = {"version": 1, "items": []}
        except Exception:
            data = {"version": 1, "items": []}
        self._gallery = data
        return data

    def _gallery_save(self):
        try:
            os.makedirs(self.gallery_dir, exist_ok=True)
            data = self._gallery or {"version": 1, "items": []}
            tmp = self.gallery_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.gallery_file)
        except Exception:
            logger.exception("保存画册失败")

    def _gallery_add(self, subject, path, w, h, sloppy, source):
        try:
            data = self._gallery_data()
            item = {
                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "subject": str(subject)[:60],
                "file": os.path.basename(path),
                "w": int(w),
                "h": int(h),
                "sloppy": bool(sloppy),
                "source": source,
            }
            data["items"].append(item)
            if len(data["items"]) > self.gallery_limit:
                data["items"] = data["items"][-self.gallery_limit:]
            self._gallery_save()
            return item
        except Exception:
            logger.exception("写入画册失败")
            return None

    def _gallery_find(self, keyword="", limit=3):
        items = list(self._gallery_data().get("items", []))
        kw = (keyword or "").strip().lower()
        if kw:
            items = [x for x in items if kw in str(x.get("subject", "")).lower()]
        return items[-limit:]

    def _gallery_count(self):
        return len(self._gallery_data().get("items", []))

    def _gallery_recent_text(self, limit=3):
        items = self._gallery_data().get("items", [])[-limit:]
        if not items:
            return "（还没画过什么）"
        return "；".join(f"{x.get('time','')[:10]} 画了「{x.get('subject','')}」" for x in items)

    # ---------- 取模型 ----------
    def _get_provider(self):
        if self.provider_id:
            try:
                p = self.context.get_provider_by_id(self.provider_id)
                if p:
                    return p
            except Exception:
                logger.warning("指定的 provider 不可用，回退到默认模型")
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    # ---------- 让模型「想画面」 ----------
    async def _plan(self, desc: str, width: int = 0, height: int = 0, force: bool = False) -> dict:
        provider = self._get_provider()
        if provider is None:
            raise RuntimeError("没有可用的对话模型，检查 AstrBot 的模型配置")
        w = int(width or self.canvas_size)
        h = int(height or self.canvas_size)
        sys_prompt = SYSTEM_PROMPT.replace("__W__", str(w)).replace("__H__", str(h))
        if self.mood_hint:
            sys_prompt += f"\n\n【你最近的状态】{self.mood_hint}"
        prompt = f"请画：{desc}"
        if force:
            prompt += (
                "\n（这是系统要用的一张配图，不是聊天里对方点的。"
                "描述可能又长又杂，请只画最核心的那一个主体，"
                "忽略光线、画质、风格、参数这类词。mood 填 normal，ops 不要为空）"
            )
        try:
            resp = await provider.text_chat(prompt=prompt, system_prompt=sys_prompt, contexts=[])
        except TypeError:
            resp = await provider.text_chat(prompt=prompt, system_prompt=sys_prompt)
        text = getattr(resp, "completion_text", None) or str(resp)
        plan = self._extract_plan(text)
        plan["ops"] = plan["ops"][: self.max_ops]
        return plan

    @staticmethod
    def _extract_plan(text: str) -> dict:
        """解析她的回答。优先按「对象」解析（带 mood/show_process），也兼容直接给数组。"""
        t = (text or "").strip()
        t = re.sub(r"^```(?:json)?", "", t).strip()
        t = re.sub(r"```$", "", t).strip()

        def _pick_ops(obj):
            ops = obj.get("ops") or obj.get("instructions") or []
            if not isinstance(ops, list):
                ops = []
            return [x for x in ops if isinstance(x, dict)]

        # ① 纯数组：模型直接给了指令列表（兼容旧格式）
        if t.startswith("["):
            e = t.rfind("]")
            if e > 0:
                try:
                    arr = json.loads(t[0 : e + 1])
                    if isinstance(arr, list):
                        return {
                            "mood": "normal",
                            "show_process": False,
                            "note": "",
                            "ops": [x for x in arr if isinstance(x, dict)],
                        }
                except Exception:
                    pass

        # ② 对象格式（带 mood / show_process）
        s, e = t.find("{"), t.rfind("}")
        if s != -1 and e > s:
            try:
                obj = json.loads(t[s : e + 1])
                if isinstance(obj, dict) and (
                    "ops" in obj or "instructions" in obj or "mood" in obj
                ):
                    mood = str(obj.get("mood") or "normal").strip().lower()
                    if mood not in ("eager", "normal", "lazy", "refuse"):
                        mood = "normal"
                    return {
                        "mood": mood,
                        "show_process": bool(obj.get("show_process", False)),
                        "note": str(obj.get("note") or "").strip()[:120],
                        "ops": _pick_ops(obj),
                    }
            except Exception:
                pass

        # ③ 兜底：从混着文字的内容里抠数组
        s, e = t.find("["), t.rfind("]")
        if s != -1 and e > s:
            try:
                arr = json.loads(t[s : e + 1])
                if isinstance(arr, list):
                    return {
                        "mood": "normal",
                        "show_process": False,
                        "note": "",
                        "ops": [x for x in arr if isinstance(x, dict)],
                    }
            except Exception:
                pass

        raise ValueError("模型没给出有效的绘制内容（可以再试一次）")

    # ---------- 工具 ----------
    def _pt(self, x, y):
        s = self.canvas_size - 1
        try:
            return (max(0, min(s, int(float(x)))), max(0, min(s, int(float(y)))))
        except Exception:
            return (0, 0)

    @staticmethod
    def _color(v):
        if not isinstance(v, str):
            return None
        v = v.strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", v) or re.fullmatch(r"#[0-9a-fA-F]{3}", v):
            return v
        return NAMED_COLORS.get(v.lower())

    @staticmethod
    def _num(v, default=0):
        try:
            return int(float(v))
        except Exception:
            return default

    def _font(self, size: int):
        candidates = []
        if self.font_path:
            candidates.append(self.font_path)
        candidates.extend(FONT_CANDIDATES)
        for p in candidates:
            if p and os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size), True
                except Exception:
                    continue
        return ImageFont.load_default(), False

    # ---------- 手绘笔触 ----------
    def _jitter_line(self, draw, p1, p2, color, width):
        x1, y1 = p1
        x2, y2 = p2
        j = self.jitter
        dist = math.hypot(x2 - x1, y2 - y1)
        n = max(2, min(24, int(dist / 10)))
        pts = [(x1 + random.uniform(-j, j), y1 + random.uniform(-j, j))]
        for i in range(1, n):
            t = i / n
            pts.append(
                (
                    x1 + (x2 - x1) * t + random.uniform(-j, j),
                    y1 + (y2 - y1) * t + random.uniform(-j, j),
                )
            )
        pts.append((x2 + random.uniform(-j, j), y2 + random.uniform(-j, j)))
        draw.line(pts, fill=color, width=width, joint="curve")

    def _jitter_polyline(self, draw, pts, color, width):
        if not pts:
            return
        if len(pts) == 1:
            x, y = pts[0]
            draw.ellipse([x - width, y - width, x + width, y + width], fill=color)
            return
        for i in range(len(pts) - 1):
            self._jitter_line(draw, pts[i], pts[i + 1], color, width)

    def _jitter_ellipse(self, draw, cx, cy, rx, ry, color, width, start=0, end=360):
        steps = max(28, int((abs(rx) + abs(ry)) / 3))
        j = self.jitter
        pts = []
        for i in range(steps + 1):
            a = math.radians(start + (end - start) * i / steps)
            pts.append(
                (
                    cx + rx * math.cos(a) + random.uniform(-j, j),
                    cy + ry * math.sin(a) + random.uniform(-j, j),
                )
            )
        draw.line(pts, fill=color, width=width, joint="curve")

    def _parse_size(self, s) -> tuple:
        """把 '1024x1024' / '2:3' / '16:9' 解析成 (宽, 高)，认不出就用方形默认。"""
        base = self.canvas_size
        if not s:
            return base, base
        t = str(s).strip().lower()
        m = re.search(r"(\d+)\s*[x*×]\s*(\d+)", t)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if w > 0 and h > 0:
                cap = 1280
                scale = min(1.0, cap / max(w, h))
                return max(64, int(w * scale)), max(64, int(h * scale))
        m = re.search(r"(\d+)\s*[:：]\s*(\d+)", t)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > 0 and b > 0:
                if a >= b:
                    return base, max(64, int(base * b / a))
                return max(64, int(base * a / b)), base
        return base, base

    # ---------- 渲染 ----------
    def _render(self, ops: list, width: int = 0, height: int = 0, sign: bool = True) -> str:
        w = max(64, int(width or self.canvas_size))
        h = max(64, int(height or self.canvas_size))
        img = PILImage.new("RGB", (w, h), PAPER)
        draw = ImageDraw.Draw(img)

        def P(x, y):
            try:
                return (
                    max(0, min(w - 1, int(float(x)))),
                    max(0, min(h - 1, int(float(y)))),
                )
            except Exception:
                return (0, 0)

        for op in ops:
            kind = str(op.get("op", "")).strip().lower()
            try:
                if kind in ("circle", "ellipse"):
                    cx, cy = P(op.get("cx", 0), op.get("cy", 0))
                    if kind == "circle":
                        rx = ry = self._num(op.get("r", 40), 40)
                    else:
                        rx = self._num(op.get("rx", 40), 40)
                        ry = self._num(op.get("ry", 40), 40)
                    rx, ry = abs(rx), abs(ry)
                    fill = self._color(op.get("fill")) or NAMED_COLORS["sky"]
                    outline = self._color(op.get("outline")) or INK
                    width = self._num(op.get("width", 3), 3)
                    draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=fill)
                    self._jitter_ellipse(draw, cx, cy, rx, ry, outline, width)

                elif kind == "rect":
                    x, y = P(op.get("x", 0), op.get("y", 0))
                    rw = abs(self._num(op.get("w", 80), 80))
                    rh = abs(self._num(op.get("h", 60), 60))
                    fill = self._color(op.get("fill")) or NAMED_COLORS["cream"]
                    outline = self._color(op.get("outline")) or INK
                    width = self._num(op.get("width", 3), 3)
                    draw.rectangle([x, y, x + rw, y + rh], fill=fill)
                    corners = [(x, y), (x + rw, y), (x + rw, y + rh), (x, y + rh), (x, y)]
                    self._jitter_polyline(draw, corners, outline, width)

                elif kind in ("line", "polyline"):
                    pts = [
                        P(p[0], p[1])
                        for p in (op.get("points") or [])
                        if isinstance(p, (list, tuple)) and len(p) >= 2
                    ]
                    color = self._color(op.get("color")) or INK
                    self._jitter_polyline(draw, pts, color, self._num(op.get("width", 3), 3))

                elif kind == "polygon":
                    pts = [
                        P(p[0], p[1])
                        for p in (op.get("points") or [])
                        if isinstance(p, (list, tuple)) and len(p) >= 2
                    ]
                    if len(pts) >= 3:
                        fill = self._color(op.get("fill")) or NAMED_COLORS["yellow"]
                        outline = self._color(op.get("outline")) or INK
                        width = self._num(op.get("width", 3), 3)
                        draw.polygon(pts, fill=fill)
                        self._jitter_polyline(draw, pts + [pts[0]], outline, width)

                elif kind == "arc":
                    cx, cy = P(op.get("cx", 0), op.get("cy", 0))
                    r = abs(self._num(op.get("r", 40), 40))
                    start = float(op.get("start", 0) or 0)
                    end = float(op.get("end", 180) or 180)
                    color = self._color(op.get("color")) or INK
                    self._jitter_ellipse(
                        draw, cx, cy, r, r, color, self._num(op.get("width", 3), 3), start, end
                    )

                elif kind == "text":
                    x, y = P(op.get("x", 0), op.get("y", 0))
                    txt = str(op.get("text", ""))[:40]
                    if txt:
                        font, _ = self._font(max(12, self._num(op.get("size", 28), 28)))
                        color = self._color(op.get("color")) or INK
                        draw.text((x, y), txt, fill=color, font=font)

                else:
                    logger.debug("跳过不认识的指令: %s", kind)
            except Exception as e:
                logger.warning("绘制指令失败 %s: %s", kind, e)

        if sign:
            self._sign(img)
        path = os.path.abspath(
            os.path.join(self.image_dir, f"doodle_{int(time.time() * 1000)}.png")
        )
        img.save(path)
        return path

    def _render_sketch(self, ops: list, width: int = 0, height: int = 0) -> str:
        """只画线稿：去掉所有填充，线条调浅，用于「起稿」那一张。"""
        light = []
        for op in ops:
            o = dict(op)
            o.pop("fill", None)
            if "outline" in o:
                o["outline"] = "#b8b8b8"
            if "color" in o:
                o["color"] = "#b8b8b8"
            o["width"] = max(2, self._num(o.get("width", 3), 3) - 1)
            light.append(o)
        return self._render(light, width, height, sign=False)

    def _sign(self, img):
        """右下角签名，像她画完随手签的名。"""
        draw = ImageDraw.Draw(img)
        base = min(img.width, img.height)
        body = max(18, base // 30)
        font, is_cjk = self._font(body)
        name = self.signature if is_cjk else "Xiaoyu"
        text = f"— {name}"
        if self.show_date:
            text += f"   {datetime.date.today():%Y.%m.%d}"
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw, th = len(text) * body // 2, body
        margin = max(16, base // 32)
        draw.text((img.width - tw - margin, img.height - th - margin), text,
                  fill="#6b6b6b", font=font)

    # ---------- 命令 ----------
    @command("涂鸦")
    async def doodle(self, event: AstrMessageEvent, desc: str = ""):
        if not self._allowed(event):
            return
        await self._ensure_services()
        desc = (desc or "").strip()
        if not desc:
            yield event.plain_result(
                "🖍️ 想让我画什么呀？\n用法：/涂鸦 一只在睡觉的鲸鱼\n"
                "想换形状：/涂鸦 16:9 一片海　/涂鸦 2:3 一棵树\n"
                "（我会自己想着画，画得丑别笑我）"
            )
            return

        # 支持 /涂鸦 16:9 一只鲸鱼 这种写法
        w = h = 0
        m = re.match(r"^(?:比例\s*[:：]?\s*)?(\d+\s*[:：x×*]\s*\d+)\s+(.+)$", desc)
        if m:
            w, h = self._parse_size(m.group(1))
            desc = m.group(2).strip()

        try:
            plan = await self._plan(desc, w, h)
        except Exception as e:
            logger.exception("涂鸦失败")
            yield event.plain_result(f"❌ 画失败了：{e}")
            return

        if plan["mood"] == "refuse":
            yield event.plain_result(plan.get("note") or "…今天不想画。")
            return

        ops = plan["ops"]
        if not ops:
            yield event.plain_result("（这次没画出来，她也没说什么）")
            return

        # 要不要给你看起稿过程，她自己决定
        if plan["show_process"]:
            try:
                yield event.plain_result("我起个稿…")
                yield event.chain_result([MsgImage(file=self._render_sketch(ops, w, h))])
            except Exception:
                logger.exception("草稿渲染失败")

        try:
            path = self._render(ops, w, h)
        except Exception as e:
            logger.exception("涂鸦失败")
            yield event.plain_result(f"❌ 画失败了：{e}")
            return

        self._gallery_add(
            desc,
            path,
            w or self.canvas_size,
            h or self.canvas_size,
            plan["mood"] == "lazy",
            "command",
        )
        yield event.chain_result([MsgImage(file=path)])

    # ---------- LLM 工具：她自己决定要不要画 ----------
    @llm_tool(name="draw_doodle")
    async def draw_doodle_tool(self, event: AstrMessageEvent, subject: str) -> str:
        """用简笔画把你想画的画面画出来，画完会自动发给对方。

        这是你自己的本事：你随时可以画，不用等对方下命令。
        当对方说「画个XX」「画一只XX给我看」，或者你自己想画点什么分享时，都可以调用。
        你画得不专业，但很有自己的味道。你画过的都会留在你的画册里，以后还能翻出来。

        Args:
            subject(string): 要画的内容，用一句简单话描述，例如「一只在睡觉的鲸鱼」
        """
        subject = (subject or "").strip()
        if not subject:
            return "没说要画什么，先问问对方想看什么吧。"

        await self._ensure_services()

        try:
            plan = await self._plan(subject)
        except Exception as e:
            logger.exception("LLM 涂鸦失败")
            return f"画失败了：{e}"

        if plan["mood"] == "refuse":
            reason = plan.get("note") or "没什么兴致"
            return (
                f"你现在不想画（理由：{reason}）。用你自己的语气回绝对方，"
                "可以带点小脾气，别道歉太刻意，也别多解释。不要画图。"
            )

        ops = plan["ops"]
        if not ops:
            return "这次没画出来。用一句话告诉对方没画成，别编理由。"

        if plan["show_process"]:
            try:
                await event.send(
                    event.chain_result([MsgImage(file=self._render_sketch(ops))])
                )
            except Exception:
                logger.exception("草稿发送失败")

        try:
            path = self._render(ops)
        except Exception as e:
            logger.exception("LLM 涂鸦失败")
            return f"画失败了：{e}"

        try:
            await event.send(event.chain_result([MsgImage(file=path)]))
        except Exception as e:
            logger.exception("发送涂鸦失败")
            return f"画好了但发送失败：{e}"

        self._gallery_add(
            subject,
            path,
            self.canvas_size,
            self.canvas_size,
            plan["mood"] == "lazy",
            "tool",
        )
        return f"画好了，已经发给对方（内容：{subject}）。可以顺口说一句你画了什么。"

    @llm_tool(name="recall_doodle")
    async def recall_doodle_tool(self, event: AstrMessageEvent, keyword: str = "") -> str:
        """翻你自己以前画过的画，挑一张发给对方看。

        当对方说「你以前画的那张呢」「给我看看你画过的」，或者你自己想提起某张旧作时使用。
        你的画册里记着每次画了什么、什么时候画的。

        Args:
            keyword(string): 想找的内容关键词，比如「鲸鱼」「海」；留空表示最近画的那张
        """
        kw = (keyword or "").strip().lower()
        item = None
        for it in reversed(self._gallery_data().get("items", [])):
            if not kw or kw in str(it.get("subject", "")).lower():
                item = it
                break

        if item is None:
            total = self._gallery_count()
            if total == 0:
                return "你的画册还是空的，一张都还没画过。"
            return f"画册里有 {total} 张，但没找到跟「{keyword}」相关的。"

        p = os.path.join(self.image_dir, item["file"])
        if not os.path.isfile(p):
            return "那张画的文件好像找不到了…"
        try:
            await event.send(event.chain_result([MsgImage(file=p)]))
        except Exception as e:
            return f"发送失败：{e}"
        return f"翻出了 {item['time']} 画的那张「{item['subject']}」，已经发给对方。"

    # ---------- 本地生图网关：冒充魔搭风格的生图 API ----------
    @staticmethod
    def _json(payload, status=200):
        return web.json_response(payload, status=status)

    async def _start_gateway(self):
        if self._gateway_runner is not None or self._gateway_starting:
            return
        if web is None:
            logger.error("缺少 aiohttp，无法启动涂鸦网关")
            return
        self._gateway_starting = True
        try:
            app = web.Application()
            app.router.add_post("/v1/images/generations", self._gw_generate)
            app.router.add_get("/v1/tasks/{tid}", self._gw_task)
            app.router.add_get("/images/{name}", self._gw_image)
            app.router.add_get("/v1/models", self._gw_models)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, self.gw_host, self.gw_port)
            await site.start()
            self._gateway_runner = runner
            logger.info("涂鸦生图网关已启动: http://%s:%s", self.gw_host, self.gw_port)
        except Exception:
            logger.exception("涂鸦生图网关启动失败")
        finally:
            self._gateway_starting = False

    async def _gw_models(self, request):
        return self._json(
            {"object": "list", "data": [{"id": "xiaoyu-doodle", "object": "model"}]}
        )

    async def _gw_generate(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        prompt = str(body.get("prompt") or body.get("input") or "").strip()
        if not prompt:
            return self._json({"error": {"message": "prompt 不能为空"}}, 400)

        size_raw = body.get("size")
        if not size_raw and body.get("width") and body.get("height"):
            size_raw = f"{body.get('width')}x{body.get('height')}"
        width, height = self._parse_size(size_raw)

        tid = "doodle-" + uuid.uuid4().hex[:16]
        self._tasks[tid] = {"status": "PROCESSING"}
        # 后台慢慢画，不阻塞 HTTP 请求
        asyncio.get_running_loop().create_task(self._gw_work(tid, prompt, width, height))

        # 默认按异步处理：多数客户端都是「先拿 task_id 再轮询」；
        # 只有显式声明 Async-Mode: false 时才走同步等待。
        async_mode = request.headers.get("X-ModelScope-Async-Mode", "true").lower() != "false"
        if async_mode:
            return self._json({"task_id": tid})

        # 同步兜底：最多等 90 秒
        for _ in range(90):
            await asyncio.sleep(1)
            st = self._tasks.get(tid, {})
            if st.get("status") == "SUCCEED":
                url = st["images"][0]
                return self._json({"images": [{"url": url}], "data": [{"url": url}]})
            if st.get("status") == "FAILED":
                return self._json({"error": {"message": st.get("error", "生成失败")}}, 500)
        return self._json({"error": {"message": "生成超时"}}, 504)

    async def _gw_task(self, request):
        tid = request.match_info["tid"]
        st = self._tasks.get(tid)
        if not st:
            return self._json({"error": {"message": "task not found"}}, 404)
        if st["status"] == "SUCCEED":
            return self._json(
                {"task_id": tid, "task_status": "SUCCEED", "output_images": st["images"]}
            )
        if st["status"] == "FAILED":
            return self._json(
                {"task_id": tid, "task_status": "FAILED", "error": st.get("error", "")}
            )
        return self._json({"task_id": tid, "task_status": "PROCESSING"})

    async def _gw_work(self, tid, prompt, width=0, height=0):
        try:
            plan = await self._plan(prompt, width, height, force=True)
            ops = plan.get("ops") or []
            if not ops:
                raise RuntimeError("这次没能画出内容")
            path = self._render(ops, width, height)
            name = os.path.basename(path)
            base = self.gw_base_url or f"http://{self.gw_host}:{self.gw_port}"
            self._tasks[tid] = {"status": "SUCCEED", "images": [f"{base}/images/{name}"]}
            self._gallery_add(
                prompt,
                path,
                width or self.canvas_size,
                height or self.canvas_size,
                False,
                "gateway",
            )
            logger.info("涂鸦网关出图: %s -> %s", prompt[:30], name)
        except Exception as e:
            logger.exception("涂鸦网关生成失败")
            self._tasks[tid] = {"status": "FAILED", "error": str(e)}

    async def _gw_image(self, request):
        name = os.path.basename(request.match_info["name"])
        path = os.path.abspath(os.path.join(self.image_dir, name))
        if not os.path.isfile(path):
            return web.Response(status=404, text="not found")
        return web.FileResponse(path)

    # ---------- 画册网页 ----------
    def _web_ok(self, request) -> bool:
        if not self.web_password:
            return False
        pwd = request.query.get("pwd") or request.headers.get("X-Pwd") or ""
        return pwd == self.web_password

    async def _start_web(self):
        if self._web_runner is not None or self._web_starting:
            return
        if web is None:
            logger.error("缺少 aiohttp，无法启动画册网页")
            return
        self._web_starting = True
        try:
            app = web.Application()
            app.router.add_get("/", self._web_index)
            app.router.add_get("/api/list", self._web_api_list)
            app.router.add_post("/api/update", self._web_api_update)
            app.router.add_post("/api/delete", self._web_api_delete)
            app.router.add_get("/api/export", self._web_api_export)
            app.router.add_get("/img/{name}", self._web_img)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, self.web_host, self.web_port).start()
            self._web_runner = runner
            logger.info("画册网页已启动: http://%s:%s", self.web_host, self.web_port)
        except Exception:
            logger.exception("画册网页启动失败")
        finally:
            self._web_starting = False

    async def _web_index(self, request):
        p = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "web", "index.html"
        )
        if not os.path.isfile(p):
            return web.Response(status=500, text="web/index.html 缺失，请检查插件文件")
        return web.FileResponse(p)

    async def _web_api_list(self, request):
        if not self._web_ok(request):
            return self._json({"ok": False, "error": "密码不对"}, 401)
        items = list(self._gallery_data().get("items", []))
        items.reverse()  # 最新的排前面
        return self._json({"ok": True, "count": len(items), "items": items})

    async def _web_api_update(self, request):
        if not self._web_ok(request):
            return self._json({"ok": False, "error": "密码不对"}, 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        fname = os.path.basename(str(body.get("file") or ""))
        if not fname:
            return self._json({"ok": False, "error": "缺少 file"}, 400)
        hit = None
        for it in self._gallery_data().get("items", []):
            if it.get("file") == fname:
                hit = it
                break
        if hit is None:
            return self._json({"ok": False, "error": "找不到这张画"}, 404)
        if "subject" in body:
            hit["subject"] = str(body.get("subject") or "")[:60]
        if "note" in body:
            hit["note"] = str(body.get("note") or "")[:200]
        self._gallery_save()
        return self._json({"ok": True, "item": hit})

    async def _web_api_delete(self, request):
        if not self._web_ok(request):
            return self._json({"ok": False, "error": "密码不对"}, 401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        fname = os.path.basename(str(body.get("file") or ""))
        data = self._gallery_data()
        before = len(data.get("items", []))
        data["items"] = [x for x in data.get("items", []) if x.get("file") != fname]
        if len(data["items"]) == before:
            return self._json({"ok": False, "error": "找不到这张画"}, 404)
        self._gallery_save()
        try:
            root = os.path.abspath(self.image_dir)
            p = os.path.abspath(os.path.join(root, fname))
            if p.startswith(root) and os.path.isfile(p):
                os.remove(p)
        except Exception:
            logger.exception("删除图片文件失败")
        return self._json({"ok": True})

    async def _web_api_export(self, request):
        if not self._web_ok(request):
            return web.Response(status=401, text="密码不对")
        import io
        import zipfile

        buf = io.BytesIO()
        items = self._gallery_data().get("items", [])
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(
                "gallery.json",
                json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=1),
            )
            for it in items:
                p = os.path.abspath(os.path.join(self.image_dir, it.get("file", "")))
                if os.path.isfile(p):
                    z.write(p, arcname="images/" + os.path.basename(p))
        return web.Response(
            body=buf.getvalue(),
            headers={
                "Content-Type": "application/zip",
                "Content-Disposition": 'attachment; filename="xiaoyu_gallery.zip"',
            },
        )

    async def _web_img(self, request):
        if not self._web_ok(request):
            return web.Response(status=401, text="密码不对")
        name = os.path.basename(request.match_info["name"])
        root = os.path.abspath(self.image_dir)
        p = os.path.abspath(os.path.join(root, name))
        if not p.startswith(root) or not os.path.isfile(p):
            return web.Response(status=404, text="not found")

        if request.query.get("thumb"):
            t = self._ensure_thumb(p)
            if t:
                return web.FileResponse(t)

        resp = web.FileResponse(p)
        if request.query.get("dl"):
            resp.headers["Content-Disposition"] = f'attachment; filename="{name}"'
        return resp

    def _ensure_thumb(self, path, max_side=360):
        """生成并缓存缩略图，列表加载快很多。失败就回退原图。"""
        try:
            d = os.path.join(self.gallery_dir, "thumbs")
            os.makedirs(d, exist_ok=True)
            out = os.path.join(d, os.path.basename(path))
            if os.path.isfile(out) and os.path.getmtime(out) >= os.path.getmtime(path):
                return out
            im = PILImage.open(path)
            im.thumbnail((max_side, max_side))
            im.save(out, "PNG", optimize=True)
            return out
        except Exception:
            logger.warning("生成缩略图失败，回退原图", exc_info=True)
            return None

    # ---------- AstrBot 插件视图 API（面板内直接管画册） ----------
    def _register_gallery_apis(self):
        if json_response is None or not hasattr(self.context, "register_web_api"):
            logger.info("当前 AstrBot 不支持插件视图 API，跳过画册面板注册")
            return
        apis = [
            (f"/{PLUGIN_NAME}/gallery/list", self.api_gallery_list, ["GET"], "画册列表"),
            (f"/{PLUGIN_NAME}/gallery/img", self.api_gallery_img, ["GET"], "画册图片"),
            (f"/{PLUGIN_NAME}/gallery/update", self.api_gallery_update, ["GET"], "修改说明"),
            (f"/{PLUGIN_NAME}/gallery/delete", self.api_gallery_delete, ["GET"], "删除这张"),
            (f"/{PLUGIN_NAME}/gallery/export", self.api_gallery_export, ["GET"], "导出全部"),
        ]
        for route, fn, methods, desc in apis:
            try:
                self.context.register_web_api(route, fn, methods, desc)
            except Exception:
                logger.exception("注册 %s 失败", route)

    async def api_gallery_list(self):
        items = list(self._gallery_data().get("items", []))
        items.reverse()  # 新的排前面
        return json_response({"ok": True, "count": len(items), "items": items})

    async def api_gallery_img(self):
        import base64

        name = os.path.basename(str(web_request.query.get("file", "") or ""))
        root = os.path.abspath(self.image_dir)
        p = os.path.abspath(os.path.join(root, name))
        if not p.startswith(root) or not os.path.isfile(p):
            return error_response("找不到这张画")
        with open(p, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return json_response({"ok": True, "data": f"data:image/png;base64,{b64}"})

    async def api_gallery_update(self):
        fname = os.path.basename(str(web_request.query.get("file", "") or ""))
        if not fname:
            return error_response("缺少 file")
        hit = None
        for it in self._gallery_data().get("items", []):
            if it.get("file") == fname:
                hit = it
                break
        if hit is None:
            return error_response("找不到这张画")
        subject = web_request.query.get("subject", None)
        note = web_request.query.get("note", None)
        if subject is not None:
            hit["subject"] = str(subject)[:60]
        if note is not None:
            hit["note"] = str(note)[:200]
        self._gallery_save()
        return json_response({"ok": True, "item": hit})

    async def api_gallery_delete(self):
        fname = os.path.basename(str(web_request.query.get("file", "") or ""))
        data = self._gallery_data()
        before = len(data.get("items", []))
        data["items"] = [x for x in data.get("items", []) if x.get("file") != fname]
        if len(data["items"]) == before:
            return error_response("找不到这张画")
        self._gallery_save()
        try:
            root = os.path.abspath(self.image_dir)
            p = os.path.abspath(os.path.join(root, fname))
            if p.startswith(root) and os.path.isfile(p):
                os.remove(p)
        except Exception:
            logger.exception("删除图片文件失败")
        return json_response({"ok": True})

    async def api_gallery_export(self):
        import io
        import base64
        import zipfile

        buf = io.BytesIO()
        items = self._gallery_data().get("items", [])
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(
                "gallery.json",
                json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=1),
            )
            for it in items:
                p = os.path.abspath(os.path.join(self.image_dir, it.get("file", "")))
                if os.path.isfile(p):
                    z.write(p, arcname="images/" + os.path.basename(p))
        return json_response(
            {
                "ok": True,
                "name": "xiaoyu_gallery.zip",
                "data": base64.b64encode(buf.getvalue()).decode(),
            }
        )
