"""指挥台展示层资源：中文业务名映射 + 共享拓扑渲染 + 拓扑独立页。

这里是**纯展示层**：

* ``ui_display.json`` 保存中文业务名映射，未知标识安全回退原始值；
* ``DISPLAY_JS`` 是主页面与拓扑页共用的中文渲染助手；
* ``TOPOLOGY_JS`` 是主页面预览与独立大图共用的唯一拓扑生成逻辑；
* ``TOPOLOGY_PAGE_HTML`` 是可直接刷新、独立访问的矢量关系图页面。

不修改任何业务对象、动作 ID、参数 JSON、角色 value 或 API 契约。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "DISPLAY",
    "label",
    "display_json",
    "DISPLAY_JS",
    "TOPOLOGY_JS",
    "TOPOLOGY_PAGE_HTML",
]

_DISPLAY_PATH = Path(__file__).with_name("ui_display.json")
DISPLAY: dict[str, Any] = json.loads(_DISPLAY_PATH.read_text(encoding="utf-8"))


def label(group: str, key: str) -> str:
    """返回中文展示名；未知标识回退原始 key，绝不空白。"""
    table = DISPLAY.get(group) or {}
    return str(table.get(key) or key)


def display_json() -> str:
    return json.dumps(DISPLAY, ensure_ascii=False)


DISPLAY_JS = r"""
const DISPLAY = __DISPLAY_JSON__;
function esc(v) {
  return String(v === null || v === undefined ? '' : v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
function zh(group, key, fallback) {
  const table = (DISPLAY && DISPLAY[group]) || {};
  if (key === undefined || key === null || key === '') {
    return fallback === undefined ? '' : fallback;
  }
  return table[key] || (fallback === undefined ? String(key) : fallback);
}
function zhField(objectType, fieldName) {
  const overrides = (DISPLAY && DISPLAY.field_overrides) || {};
  const specific = overrides[objectType + '.' + fieldName];
  if (specific) return specific;
  return ((DISPLAY && DISPLAY.fields) || {})[fieldName] || fieldName;
}
function zhValue(fieldName, value) {
  if (value === null || value === undefined) return value;
  if (['status', 'stage', 'availability', 'verdict'].indexOf(fieldName) < 0) {
    return value;
  }
  return zh('statuses', String(value), value);
}
function zhRole(role) { return zh('roles', role, role); }
function zhKind(kind) { return zh('kinds', kind, kind); }
function zhObject(name) { return zh('objects', name, name); }
function zhAction(name) { return zh('actions', name, name); }
function zhCardinality(value) { return zh('cardinalities', value, value); }
function zhStatus(value) { return zh('statuses', value, value); }
"""


TOPOLOGY_JS = r"""
function topologyScene(m, W, H, fs) {
  fs = fs || 1;
  W = W || 900;
  H = H || 520;
  const types = ((m && m.object_types) || []).map(t => t.name);
  const links = (m && m.link_types) || [];
  const cx = W / 2, cy = H / 2;
  const rx = Math.max(140 * fs, W / 2 - 260 * fs);
  const ry = Math.max(100 * fs, H / 2 - 150 * fs);
  const pos = {};
  if (types.length === 1) {
    pos[types[0]] = {x: cx, y: cy};
  } else if (types.length === 2) {
    pos[types[0]] = {x: cx - rx, y: cy};
    pos[types[1]] = {x: cx + rx, y: cy};
  } else {
    types.forEach((name, i) => {
      const angle = -Math.PI / 2 + i * 2 * Math.PI / types.length;
      pos[name] = {x: cx + rx * Math.cos(angle), y: cy + ry * Math.sin(angle)};
    });
  }

  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  function grow(x1, y1, x2, y2) {
    minX = Math.min(minX, x1); minY = Math.min(minY, y1);
    maxX = Math.max(maxX, x2); maxY = Math.max(maxY, y2);
  }

  const metrics = {};
  types.forEach(name => {
    const zhName = zhObject(name);
    const width = Math.max(
      140 * fs,
      Math.max(zhName.length * 13 * fs, name.length * 7 * fs) + 42 * fs
    );
    metrics[name] = {zh: zhName, en: name, w: width, h: 54 * fs};
    const p = pos[name];
    grow(p.x - width / 2, p.y - 27 * fs, p.x + width / 2, p.y + 27 * fs);
  });

  const pairCounts = {};
  links.forEach(l => {
    const key = [l.source, l.target].sort().join('|');
    pairCounts[key] = (pairCounts[key] || 0) + 1;
  });
  const seen = {};
  let lineParts = '';
  let labelParts = '';
  // 从矩形内部的起点射向对端，求射线离开「以 center 为中心的矩形」的边界点，
  // 让箭头严格贴在节点框边缘，而不是藏在半透明节点框内部。
  function exitPoint(start, target, center, meta) {
    const dx = target.x - start.x, dy = target.y - start.y;
    const hx = meta.w / 2 + 6 * fs, hy = meta.h / 2 + 6 * fs;
    const tx = dx === 0 ? Infinity : ((dx > 0 ? center.x + hx : center.x - hx) - start.x) / dx;
    const ty = dy === 0 ? Infinity : ((dy > 0 ? center.y + hy : center.y - hy) - start.y) / dy;
    const t = Math.max(0, Math.min(tx, ty, 1));
    return {x: start.x + dx * t, y: start.y + dy * t};
  }
  links.forEach(l => {
    const a = pos[l.source], b = pos[l.target];
    if (!a || !b) return;
    const key = [l.source, l.target].sort().join('|');
    const index = seen[key] || 0;
    seen[key] = index + 1;
    const count = pairCounts[key] || 1;
    const dx = b.x - a.x, dy = b.y - a.y;
    const len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len, ny = dx / len;
    const off = (index - (count - 1) / 2) * 30 * fs;
    // 整条线按法向平移后再裁到矩形边界：平行边彼此分离且都贴在框上。
    const a2 = {x: a.x + nx * off, y: a.y + ny * off};
    const b2 = {x: b.x + nx * off, y: b.y + ny * off};
    const p1 = exitPoint(a2, b2, a, metrics[l.source]);
    const p2 = exitPoint(b2, a2, b, metrics[l.target]);
    const x1 = p1.x, y1 = p1.y;
    const x2 = p2.x, y2 = p2.y;
    // 多条平行边时沿连线方向错开标签，避免中文长标签互相压字。
    const frac = count > 1 ? 0.5 + (index - (count - 1) / 2) * 0.24 : 0.5;
    const mx = x1 + (x2 - x1) * frac + nx * 18 * fs;
    const my = y1 + (y2 - y1) * frac + ny * 18 * fs;
    const text = zh('links', l.name, l.name) + ' · ' + zhCardinality(l.cardinality);
    const textWidth = text.length * 9.5 * fs;
    grow(mx - textWidth / 2, my - 12 * fs, mx + textWidth / 2, my + 6 * fs);
    lineParts += '<line x1="' + x1 + '" y1="' + y1 + '" x2="' + x2 + '" y2="' + y2 +
      '" stroke="rgba(56,189,248,.5)" stroke-width="' + (1.5 * fs) +
      '" marker-end="url(#arw)"/>';
    // 标签最后统一绘制，并用「描边打底 + 填充」让交叉线不糊住中文字形。
    labelParts += '<text x="' + mx + '" y="' + my + '" fill="#9fb4cc" font-size="' +
      (9.5 * fs) + '" text-anchor="middle" stroke="rgba(5,8,15,.94)" stroke-width="' +
      (3.2 * fs) + '" stroke-linejoin="round" paint-order="stroke">' + esc(text) + '</text>';
  });

  let nodes = '';
  types.forEach(name => {
    const p = pos[name];
    const meta = metrics[name];
    nodes += '<rect class="node" x="' + (p.x - meta.w / 2) + '" y="' + (p.y - meta.h / 2) +
      '" width="' + meta.w + '" height="' + meta.h + '" rx="' + (11 * fs) +
      '" fill="rgba(56,189,248,.12)" stroke="rgba(56,189,248,.6)"/>' +
      '<text x="' + p.x + '" y="' + (p.y - 1 * fs) + '" fill="#d6f2ff" font-size="' +
      (12.5 * fs) + '" font-weight="700" text-anchor="middle">' + esc(meta.zh) + '</text>' +
      '<text x="' + p.x + '" y="' + (p.y + 14 * fs) + '" fill="#7f93ab" font-size="' +
      (8.5 * fs) + '" text-anchor="middle">' + esc(meta.en) + '</text>';
  });

  const pad = 26 * fs;
  if (!types.length) {
    return {inner: '', bbox: {minX: 0, minY: 0, maxX: 1, maxY: 1}};
  }

  /* ------------------------------------------------------------------
     动态语义层：在对象关系环下方同图展示 Action / Function / Automation。
     全部坐标统一走 grow(...)，让 fit() 与 topologySvg() 的 viewBox 完整包裹。
     ------------------------------------------------------------------ */
  const actions = (m && m.actions) || [];
  const funcs = (m && m.functions) || [];
  const autos = (m && m.automations) || [];
  const pkToType = {};
  ((m && m.object_types) || []).forEach(t => {
    if (t && t.primary_key) pkToType[t.primary_key] = t.name;
  });
  /* 动作参数里若出现某个对象类型的主键，就说明该动作受控改写这个对象。 */
  function targetOf(action) {
    const props = (action && action.params && action.params.properties) || {};
    const keys = Object.keys(props);
    for (let i = 0; i < keys.length; i++) {
      if (pkToType[keys[i]]) return pkToType[keys[i]];
    }
    return '';
  }
  function glyphWidth(ch) { return ch.charCodeAt(0) > 255 ? 1 : 0.56; }
  function textWidth(text, size) {
    const s = String(text === null || text === undefined ? '' : text);
    const chars = Array.from(s);
    let w = 0;
    chars.forEach(ch => { w += glyphWidth(ch) * size; });
    return w;
  }
  function clipText(text, size, maxWidth) {
    const s = String(text === null || text === undefined ? '' : text);
    if (textWidth(s, size) <= maxWidth) return s;
    const chars = Array.from(s);
    let out = '';
    for (let i = 0; i < chars.length; i++) {
      if (textWidth(out + chars[i] + '…', size) > maxWidth) break;
      out += chars[i];
    }
    return out + '…';
  }
  function roleLine(action) {
    const roles = ((action && action.allowed_roles) || []).map(zhRole);
    const approvers = ((action && action.approver_roles) || []).map(zhRole);
    let text = roles.length ? roles.slice(0, 2).join(' / ') : '—';
    if (roles.length > 2) text += ' +' + (roles.length - 2);
    if (approvers.length) {
      text += ' · 审批 ' + approvers.slice(0, 1).join('');
      if (approvers.length > 1) text += '+' + (approvers.length - 1);
    }
    return text;
  }

  let layerParts = '';
  if (actions.length || funcs.length || autos.length) {
    const layerTop = maxY + 52 * fs;
    const panelW = Math.max(maxX - minX, 880 * fs);
    const panelLeft = cx - panelW / 2;
    const panelGap = 26 * fs;
    const actionsW = Math.round(panelW * 0.62);
    const funcsW = panelW - actionsW - panelGap;
    const panelPad = 16 * fs;
    const headH = 34 * fs;
    const gap = 14 * fs;

    const actionCols = Math.min(3, Math.max(1, actions.length));
    const actionRows = Math.max(1, Math.ceil(actions.length / actionCols));
    const actionCardW = (actionsW - panelPad * 2 - gap * (actionCols - 1)) / actionCols;
    const actionCardH = 92 * fs;
    const actionsBodyH = actions.length
      ? actionRows * actionCardH + (actionRows - 1) * gap : 0;

    const funcCols = funcs.length > 2 ? 2 : 1;
    const funcRows = Math.max(1, Math.ceil(funcs.length / funcCols));
    const funcCardW = (funcsW - panelPad * 2 - gap * (funcCols - 1)) / funcCols;
    const funcCardH = 62 * fs;
    const funcsBodyH = funcs.length
      ? funcRows * funcCardH + (funcRows - 1) * gap : 0;

    const autoCols = autos.length > 2 ? 2 : 1;
    const autoRows = Math.max(1, Math.ceil(autos.length / autoCols));
    const autoCardW = (funcsW - panelPad * 2 - gap * (autoCols - 1)) / autoCols;
    const autoCardH = 52 * fs;
    const autosBodyH = autos.length
      ? autoRows * autoCardH + (autoRows - 1) * gap : 0;

    const actionsH = panelPad * 2 + headH + (actions.length ? gap + actionsBodyH : 0);
    const funcsH = panelPad * 2 + headH +
      (funcs.length ? gap + funcsBodyH : 0) + (autos.length ? gap + autosBodyH : 0);
    const panelH = Math.max(actionsH, funcsH, 110 * fs);

    const headBase = layerTop + panelPad + 21 * fs;
    const bodyTop = layerTop + panelPad + headH + gap;
    const funcLeft = panelLeft + actionsW + panelGap;
    const autoBodyTop = bodyTop + funcsBodyH + (funcs.length ? gap : 0);

    function panelBox(x, y, w, h, tint) {
      return '<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + h +
        '" rx="' + (14 * fs) + '" fill="rgba(8,14,22,.72)" stroke="' + tint + '"/>';
    }
    function panelHead(x, y, text, color) {
      return '<text x="' + (x + panelPad) + '" y="' + y + '" fill="' + color +
        '" font-size="' + (11.5 * fs) + '" font-weight="700" letter-spacing=".6">' +
        esc(text) + '</text>';
    }

    layerParts += panelBox(panelLeft, layerTop, actionsW, panelH, 'rgba(245,165,36,.30)') +
      panelHead(panelLeft, headBase,
        '🎯 业务动作层 · GOVERNED ACTIONS · ' + actions.length, '#ffd79a');

    actions.forEach((action, index) => {
      const col = index % actionCols;
      const row = Math.floor(index / actionCols);
      const x = panelLeft + panelPad + col * (actionCardW + gap);
      const y = bodyTop + row * (actionCardH + gap);
      const innerW = actionCardW - 20 * fs;
      const tx = x + 10 * fs;
      const target = targetOf(action);
      layerParts += '<g>' +
        '<rect class="action-node" x="' + x + '" y="' + y + '" width="' + actionCardW +
        '" height="' + actionCardH + '" rx="' + (10 * fs) +
        '" fill="rgba(245,165,36,.10)" stroke="rgba(245,165,36,.65)"/>' +
        '<text x="' + tx + '" y="' + (y + 26 * fs) + '" fill="#ffe3b0" font-size="' +
        (11.5 * fs) + '" font-weight="700">' +
        esc(clipText('🎯 ' + zhAction(action.name), 11.5 * fs, innerW)) + '</text>' +
        '<text x="' + tx + '" y="' + (y + 45 * fs) + '" fill="#c9a06a" font-size="' +
        (8.5 * fs) + '">' + esc(clipText(action.name, 8.5 * fs, innerW)) + '</text>' +
        '<text x="' + tx + '" y="' + (y + 63 * fs) + '" fill="#a98b5f" font-size="' +
        (8 * fs) + '">' +
        esc(clipText('角色 ' + roleLine(action), 8 * fs, innerW)) + '</text>' +
        (target
          ? '<text x="' + tx + '" y="' + (y + 81 * fs) +
            '" fill="#7fd6c0" font-size="' + (8 * fs) + '">' +
            esc(clipText('作用于 ' + zhObject(target), 8 * fs, innerW)) + '</text>'
          : '') +
        '</g>';
    });

    layerParts += panelBox(funcLeft, layerTop, funcsW, panelH, 'rgba(167,139,250,.30)') +
      panelHead(funcLeft, headBase,
        '🧮 查询与计算函数层 · SEMANTIC FUNCTIONS · ' + funcs.length, '#d8ccff');

    funcs.forEach((fn, index) => {
      const col = index % funcCols;
      const row = Math.floor(index / funcCols);
      const x = funcLeft + panelPad + col * (funcCardW + gap);
      const y = bodyTop + row * (funcCardH + gap);
      const innerW = funcCardW - 20 * fs;
      const tx = x + 10 * fs;
      const sub = fn.description ? fn.name + ' · ' + fn.description : fn.name;
      layerParts += '<g>' +
        '<rect class="fn-node" x="' + x + '" y="' + y + '" width="' + funcCardW +
        '" height="' + funcCardH + '" rx="' + (10 * fs) +
        '" fill="rgba(167,139,250,.10)" stroke="rgba(167,139,250,.65)"/>' +
        '<text x="' + tx + '" y="' + (y + 25 * fs) + '" fill="#e3dbff" font-size="' +
        (11 * fs) + '" font-weight="700">' +
        esc(clipText('🧮 ' + zh('functions', fn.name, fn.name), 11 * fs, innerW)) + '</text>' +
        '<text x="' + tx + '" y="' + (y + 44 * fs) + '" fill="#9c8fd6" font-size="' +
        (8.5 * fs) + '">' + esc(clipText(sub, 8.5 * fs, innerW)) + '</text>' +
        '</g>';
    });

    autos.forEach((auto, index) => {
      const col = index % autoCols;
      const row = Math.floor(index / autoCols);
      const x = funcLeft + panelPad + col * (autoCardW + gap);
      const y = autoBodyTop + row * (autoCardH + gap);
      const innerW = autoCardW - 20 * fs;
      const tx = x + 10 * fs;
      layerParts += '<g>' +
        '<rect class="auto-node" x="' + x + '" y="' + y + '" width="' + autoCardW +
        '" height="' + autoCardH + '" rx="' + (10 * fs) +
        '" fill="rgba(56,189,248,.08)" stroke="rgba(56,189,248,.55)"/>' +
        '<text x="' + tx + '" y="' + (y + 23 * fs) + '" fill="#bfe9ff" font-size="' +
        (10.5 * fs) + '" font-weight="700">' +
        esc(clipText('⚡ ' + zh('automations', auto.name, auto.name), 10.5 * fs, innerW)) +
        '</text>' +
        '<text x="' + tx + '" y="' + (y + 41 * fs) + '" fill="#7f93ab" font-size="' +
        (8 * fs) + '">' + esc(clipText(auto.name, 8 * fs, innerW)) + '</text>' +
        '</g>';
    });

    grow(panelLeft, layerTop, panelLeft + panelW, layerTop + panelH);
  }

  return {
    inner: '<defs><marker id="arw" viewBox="0 0 10 10" refX="9" refY="5" ' +
      'markerWidth="7" markerHeight="7" orient="auto-start-reverse">' +
      '<path d="M0,0 L10,5 L0,10 z" fill="#38bdf8"/></marker></defs>' +
      lineParts + labelParts + nodes + layerParts,
    bbox: {
      minX: minX - pad, minY: minY - pad,
      maxX: maxX + pad, maxY: maxY + pad
    }
  };
}

function topologySvg(m) {
  const scene = topologyScene(m, 900, 520, 1);
  const b = scene.bbox;
  const w = Math.max(b.maxX - b.minX, 1);
  const h = Math.max(b.maxY - b.minY, 1);
  return '<svg viewBox="' + [b.minX, b.minY, w, h].join(' ') +
    '" preserveAspectRatio="xMidYMid meet">' + scene.inner + '</svg>';
}
"""



TOPOLOGY_PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>本体关系图 · __TITLE__</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#05080f;--panel:#0c1420;--line:rgba(120,160,210,.18);
  --txt:#e6eefb;--dim:#8ea3bd;--cy:#38bdf8}
html,body{height:100%}
body{background:
  radial-gradient(1200px 600px at 8% -10%,rgba(56,189,248,.14),transparent 62%),
  radial-gradient(900px 520px at 98% -6%,rgba(129,140,248,.12),transparent 60%),
  linear-gradient(#05080f,#02040a);
  color:var(--txt);font-family:"PingFang SC","Hiragino Sans GB","Heiti SC",sans-serif;
  overflow:hidden;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:18px;padding:12px 20px;
  border-bottom:1px solid var(--line);background:rgba(8,14,22,.9);flex-shrink:0}
.mark{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;
  font-size:19px;font-weight:800;color:#03121d;
  background:linear-gradient(135deg,#38bdf8,#6366f1);box-shadow:0 8px 22px rgba(56,189,248,.35)}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.brand b{font-size:16px;letter-spacing:.4px;display:block;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;max-width:52vw}
.brand span{display:block;font-size:10.5px;color:var(--dim);letter-spacing:1.2px;margin-top:3px}
.tools{margin-left:auto;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pct{font-family:"SF Mono",Menlo,monospace;font-size:12px;color:#a5e4ff;
  border:1px solid var(--line);border-radius:8px;padding:5px 9px;min-width:58px;text-align:center}
button,a.back{font-size:12px;padding:6px 12px;border-radius:8px;cursor:pointer;
  border:1px solid rgba(56,189,248,.42);background:rgba(56,189,248,.12);color:#cfeeff;
  text-decoration:none;white-space:nowrap}
button:hover,a.back:hover{background:rgba(56,189,248,.24)}
button:disabled{opacity:.4;cursor:not-allowed}
a.back{border-color:rgba(167,139,250,.45);background:rgba(167,139,250,.12);color:#e3dbff}
main{position:relative;flex:1;min-height:0}
#canvas{width:100%;height:100%;display:block;cursor:grab;touch-action:none;user-select:none}
#canvas.dragging{cursor:grabbing}
#empty{position:absolute;inset:0;display:grid;place-items:center;font-size:15px;
  color:var(--dim);pointer-events:none}
#error{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:10px;text-align:center;padding:24px}
#error[hidden],#empty[hidden]{display:none}
#error b{font-size:19px}
#error p{font-size:12.5px;color:var(--dim);max-width:560px;line-height:1.7}
#error a{color:#a5e4ff;font-size:13px;text-decoration:none;border-bottom:1px solid rgba(56,189,248,.4)}
.eicon{font-size:38px;color:#f5a524}
.foot{position:absolute;left:20px;bottom:14px;font-size:10.5px;color:#6f839b;
  letter-spacing:.5px;pointer-events:none}
</style></head>
<body>
<header>
  <div class="brand">
    <div class="mark">◈</div>
    <div><b id="title">本体关系图</b><span id="subtitle">正在加载本体蓝图…</span></div>
  </div>
  <div class="tools">
    <span class="pct" title="缩放比例"><b id="zoomPct">100%</b></span>
    <button id="zoomOut" title="缩小">缩小</button>
    <button id="zoomIn" title="放大">放大</button>
    <button id="fitBtn" title="适应窗口">适应窗口</button>
    <button id="resetBtn" title="复位">复位</button>
    <a class="back" id="back" href="/" target="_self" rel="noopener">返回指挥台</a>
  </div>
</header>
<main>
  <svg id="canvas"><g id="scene"></g></svg>
  <div id="empty" hidden>该本体暂无对象类型</div>
  <div id="error" hidden>
    <div class="eicon">⚠</div>
    <b id="errorTitle">未找到该本体蓝图</b>
    <p id="errorText"></p>
    <a href="/" target="_self" rel="noopener">返回指挥台</a>
  </div>
  <div class="foot">拖拽平移 · 滚轮缩放 · 双击适应窗口</div>
</main>
<script>
__UI_BUNDLE__
__TOPOLOGY_JS__
const params = new URLSearchParams(location.search);
const ontologyId = params.get('ontology') || '';
const canvas = document.getElementById('canvas');
const sceneG = document.getElementById('scene');
const pctEl = document.getElementById('zoomPct');
const MIN_SCALE = 0.12;
const MAX_SCALE = 6;
const MAX_FIT = 2.4;
let scene = null;
let userMoved = false;
const view = {scale: 1, tx: 0, ty: 0};

function clamp(value, low, high) { return Math.max(low, Math.min(high, value)); }
function viewportSize() {
  const rect = canvas.getBoundingClientRect();
  return {w: rect.width || 1, h: rect.height || 1};
}
function applyView() {
  sceneG.setAttribute(
    'transform',
    'translate(' + view.tx + ' ' + view.ty + ') scale(' + view.scale + ')'
  );
  pctEl.textContent = Math.round(view.scale * 100) + '%';
}
function fit() {
  if (!scene) return;
  const size = viewportSize();
  const b = scene.bbox;
  const bw = Math.max(b.maxX - b.minX, 1);
  const bh = Math.max(b.maxY - b.minY, 1);
  const pad = 42;
  let scale = Math.min((size.w - pad * 2) / bw, (size.h - pad * 2) / bh);
  scale = clamp(scale, MIN_SCALE, MAX_FIT);
  view.scale = scale;
  view.tx = (size.w - bw * scale) / 2 - b.minX * scale;
  view.ty = (size.h - bh * scale) / 2 - b.minY * scale;
  userMoved = false;
  applyView();
}
function zoomAt(factor, cx, cy) {
  if (!scene) return;
  const before = view.scale;
  const next = clamp(before * factor, MIN_SCALE, MAX_SCALE);
  if (next === before) return;
  const ratio = next / before;
  view.tx = cx - (cx - view.tx) * ratio;
  view.ty = cy - (cy - view.ty) * ratio;
  view.scale = next;
  userMoved = true;
  applyView();
}
function zoomCenter(factor) {
  const size = viewportSize();
  zoomAt(factor, size.w / 2, size.h / 2);
}

document.getElementById('zoomIn').addEventListener('click', () => zoomCenter(1.25));
document.getElementById('zoomOut').addEventListener('click', () => zoomCenter(1 / 1.25));
document.getElementById('fitBtn').addEventListener('click', fit);
document.getElementById('resetBtn').addEventListener('click', fit);

canvas.addEventListener('wheel', event => {
  event.preventDefault();
  const rect = canvas.getBoundingClientRect();
  zoomAt(
    Math.exp(-event.deltaY * 0.0012),
    event.clientX - rect.left,
    event.clientY - rect.top
  );
}, {passive: false});

let drag = null;
canvas.addEventListener('pointerdown', event => {
  if (event.pointerType === 'mouse' && event.button !== 0) return;
  drag = {id: event.pointerId, x: event.clientX, y: event.clientY,
          tx: view.tx, ty: view.ty};
  canvas.setPointerCapture(event.pointerId);
  canvas.classList.add('dragging');
});
canvas.addEventListener('pointermove', event => {
  if (!drag || event.pointerId !== drag.id) return;
  view.tx = drag.tx + (event.clientX - drag.x);
  view.ty = drag.ty + (event.clientY - drag.y);
  userMoved = true;
  applyView();
});
function endDrag(event) {
  if (!drag || event.pointerId !== drag.id) return;
  drag = null;
  canvas.classList.remove('dragging');
  try { canvas.releasePointerCapture(event.pointerId); } catch (err) {}
}
canvas.addEventListener('pointerup', endDrag);
canvas.addEventListener('pointercancel', endDrag);
canvas.addEventListener('lostpointercapture', endDrag);
canvas.addEventListener('dblclick', fit);

window.addEventListener('resize', () => {
  if (!scene) return;
  if (userMoved) applyView(); else fit();
});
window.addEventListener('keydown', event => {
  if (event.key === '+' || event.key === '=') zoomCenter(1.2);
  else if (event.key === '-') zoomCenter(1 / 1.2);
  else if (event.key === '0') fit();
});

function showError(title, detail) {
  document.getElementById('errorTitle').textContent = title;
  document.getElementById('errorText').textContent = detail || '';
  document.getElementById('error').hidden = false;
  document.getElementById('empty').hidden = true;
  canvas.style.display = 'none';
  document.getElementById('subtitle').textContent = '无法加载本体关系图';
  ['zoomIn', 'zoomOut', 'fitBtn', 'resetBtn'].forEach(id => {
    document.getElementById(id).disabled = true;
  });
}

async function load() {
  if (!ontologyId) {
    showError('缺少本体参数', '请从指挥台点击“查看大图”，或使用 ?ontology=<本体编号> 访问。');
    return;
  }
  try {
    const response = await fetch('/api/ontology/' + encodeURIComponent(ontologyId));
    if (response.status === 404) {
      showError('未找到该本体蓝图', '请检查链接中的本体编号，或返回指挥台重新选择。');
      return;
    }
    if (!response.ok) {
      showError('加载本体关系图失败', '服务返回 HTTP ' + response.status + '。');
      return;
    }
    const data = await response.json();
    const name = zh('ontologies', data.ontology.ontology_id, data.ontology.name);
    const category = zh('categories', data.ontology.category, data.ontology.category);
    document.getElementById('title').textContent = '本体关系图 · ' + name;
    document.title = '本体关系图 · ' + name;
    document.getElementById('subtitle').textContent =
      category + ' · ' + (data.object_types || []).length + ' 个对象类型 · ' +
      (data.link_types || []).length + ' 条关系 · ' +
      (data.actions || []).length + ' 个业务动作 · ' +
      (data.functions || []).length + ' 个查询函数';
    scene = topologyScene(data, 1200, 700, 1.5);
    sceneG.innerHTML = scene.inner;
    if (!(data.object_types || []).length) {
      document.getElementById('empty').hidden = false;
    }
    fit();
  } catch (err) {
    showError('加载本体关系图失败', String(err && err.message ? err.message : err));
  }
}

load();
</script></body></html>
"""
