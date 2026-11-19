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
    "DOCS_PAGE_HTML",
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
  const links = (m && m.object_properties) || [];
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
    const key = [l.domain, l.range].sort().join('|');
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
  links.forEach((l, linkIdx) => {
    const a = pos[l.domain], b = pos[l.range];
    if (!a || !b) return;
    const key = [l.domain, l.range].sort().join('|');
    const index = seen[key] || 0;
    seen[key] = index + 1;
    const count = pairCounts[key] || 1;
    const labelName = zh('object_properties', l.name, '') || zh('links', l.name, l.name);
    const text = labelName + ' · ' + zhCardinality(l.cardinality);
    const textWidth = text.length * 9.5 * fs;
    const edgeAttrs = 'class="topo-item" data-topo-kind="object_property" data-topo-id="' +
      linkIdx + '" data-topo-domain="' + esc(l.domain) + '" data-topo-range="' + esc(l.range) + '"';
    if (l.domain === l.range) {
      const meta = metrics[l.domain];
      const sx = a.x + meta.w * 0.22, sy = a.y - meta.h / 2 - 4 * fs;
      const ex = a.x - meta.w * 0.22, ey = sy;
      const loopH = (42 + index * 22) * fs;
      const mx = a.x, my = sy - loopH;
      grow(mx - Math.max(textWidth / 2, meta.w * 0.5), my - 14 * fs, mx + Math.max(textWidth / 2, meta.w * 0.5), sy);
      const loopD = 'M ' + sx + ' ' + sy + ' C ' + (sx + 18 * fs) + ' ' + (sy - loopH) +
        ', ' + (ex - 18 * fs) + ' ' + (ey - loopH) + ', ' + ex + ' ' + ey;
      lineParts += '<g ' + edgeAttrs + '>' +
        '<path d="' + loopD + '" fill="none" stroke="transparent" stroke-width="' + (14 * fs) + '"/>' +
        '<path class="edge-vis" d="' + loopD +
        '" fill="none" stroke="rgba(56,189,248,.5)" stroke-width="' + (1.5 * fs) +
        '" marker-end="url(#arw)"/></g>';
      labelParts += '<text ' + edgeAttrs.replace('class="topo-item"', 'class="topo-item edge-lbl"') +
        ' x="' + mx + '" y="' + (my + 6 * fs) + '" fill="#9fb4cc" font-size="' +
        (9.5 * fs) + '" text-anchor="middle" stroke="rgba(5,8,15,.94)" stroke-width="' +
        (3.2 * fs) + '" stroke-linejoin="round" paint-order="stroke">' + esc(text) + '</text>';
      return;
    }
    const dx = b.x - a.x, dy = b.y - a.y;
    const len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len, ny = dx / len;
    const off = (index - (count - 1) / 2) * 30 * fs;
    // 整条线按法向平移后再裁到矩形边界：平行边彼此分离且都贴在框上。
    const a2 = {x: a.x + nx * off, y: a.y + ny * off};
    const b2 = {x: b.x + nx * off, y: b.y + ny * off};
    const p1 = exitPoint(a2, b2, a, metrics[l.domain]);
    const p2 = exitPoint(b2, a2, b, metrics[l.range]);
    const x1 = p1.x, y1 = p1.y;
    const x2 = p2.x, y2 = p2.y;
    // 多条平行边时沿连线方向错开标签，避免中文长标签互相压字。
    const frac = count > 1 ? 0.5 + (index - (count - 1) / 2) * 0.24 : 0.5;
    const mx = x1 + (x2 - x1) * frac + nx * 18 * fs;
    const my = y1 + (y2 - y1) * frac + ny * 18 * fs;
    grow(mx - textWidth / 2, my - 12 * fs, mx + textWidth / 2, my + 6 * fs);
    lineParts += '<g ' + edgeAttrs + '>' +
      '<line x1="' + x1 + '" y1="' + y1 + '" x2="' + x2 + '" y2="' + y2 +
      '" stroke="transparent" stroke-width="' + (14 * fs) + '"/>' +
      '<line class="edge-vis" x1="' + x1 + '" y1="' + y1 + '" x2="' + x2 + '" y2="' + y2 +
      '" stroke="rgba(56,189,248,.5)" stroke-width="' + (1.5 * fs) +
      '" marker-end="url(#arw)"/></g>';
    // 标签最后统一绘制，并用「描边打底 + 填充」让交叉线不糊住中文字形。
    labelParts += '<text ' + edgeAttrs.replace('class="topo-item"', 'class="topo-item edge-lbl"') +
      ' x="' + mx + '" y="' + my + '" fill="#9fb4cc" font-size="' +
      (9.5 * fs) + '" text-anchor="middle" stroke="rgba(5,8,15,.94)" stroke-width="' +
      (3.2 * fs) + '" stroke-linejoin="round" paint-order="stroke">' + esc(text) + '</text>';
  });

  let nodes = '';
  types.forEach(name => {
    const p = pos[name];
    const meta = metrics[name];
    nodes += '<g class="topo-item" data-topo-kind="object_type" data-topo-id="' + esc(name) + '">' +
      '<rect class="node" x="' + (p.x - meta.w / 2) + '" y="' + (p.y - meta.h / 2) +
      '" width="' + meta.w + '" height="' + meta.h + '" rx="' + (11 * fs) +
      '" fill="rgba(56,189,248,.12)" stroke="rgba(56,189,248,.6)"/>' +
      '<text x="' + p.x + '" y="' + (p.y - 1 * fs) + '" fill="#d6f2ff" font-size="' +
      (12.5 * fs) + '" font-weight="700" text-anchor="middle">' + esc(meta.zh) + '</text>' +
      '<text x="' + p.x + '" y="' + (p.y + 14 * fs) + '" fill="#7f93ab" font-size="' +
      (8.5 * fs) + '" text-anchor="middle">' + esc(meta.en) + '</text></g>';
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
  const roleDefs = (m && m.role_defs) || [];
  const pkToType = {};
  ((m && m.object_types) || []).forEach(t => {
    if (t && t.primary_key) pkToType[t.primary_key] = t.name;
  });
  /* 动作参数里若出现对象类型主键，优先认定该动作作用于它；否则回落到首个 ObjectProperty 参数的目标类型。 */
  function targetOf(action) {
    const props = (action && action.params && action.params.properties) || {};
    const keys = Object.keys(props);
    for (let i = 0; i < keys.length; i++) {
      if (pkToType[keys[i]]) return pkToType[keys[i]];
    }
    for (let i = 0; i < keys.length; i++) {
      const p = props[keys[i]] || {};
      if (p.property_kind === 'object' && p.range) return p.range;
      if (Array.isArray(p.anyOf)) {
        const ref = p.anyOf.find(a => a && a.property_kind === 'object' && a.range);
        if (ref) return ref.range;
      }
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
  const panelW = Math.max(maxX - minX, 880 * fs);
  const panelLeft = cx - panelW / 2;
  const panelPad = 16 * fs;
  const headH = 34 * fs;
  const gap = 14 * fs;
  const rowGap = 18 * fs;
  let nextLayerTop = maxY + 52 * fs;

  function panelBox(x, y, w, h, tint) {
    return '<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + h +
      '" rx="' + (14 * fs) + '" fill="rgba(8,14,22,.72)" stroke="' + tint + '"/>';
  }
  function panelHead(x, y, text, color) {
    return '<text x="' + (x + panelPad) + '" y="' + y + '" fill="' + color +
      '" font-size="' + (11.5 * fs) + '" font-weight="700" letter-spacing=".6">' +
      esc(text) + '</text>';
  }

  if (actions.length || funcs.length || autos.length) {
    const layerTop = nextLayerTop;
    const panelGap = 26 * fs;
    const actionsW = Math.round(panelW * 0.62);
    const rightW = panelW - actionsW - panelGap;

    const actionCols = Math.min(3, Math.max(1, actions.length));
    const actionRows = Math.max(1, Math.ceil(actions.length / actionCols));
    const actionCardW = (actionsW - panelPad * 2 - gap * (actionCols - 1)) / actionCols;
    const actionCardH = 92 * fs;
    const actionsBodyH = actions.length
      ? actionRows * actionCardH + (actionRows - 1) * gap : 26 * fs;

    const funcCols = funcs.length > 2 ? 2 : 1;
    const funcRows = Math.max(1, Math.ceil(funcs.length / funcCols));
    const funcCardW = (rightW - panelPad * 2 - gap * (funcCols - 1)) / funcCols;
    const funcCardH = 62 * fs;
    const funcsBodyH = funcs.length
      ? funcRows * funcCardH + (funcRows - 1) * gap : 26 * fs;

    const autoCols = autos.length > 2 ? 2 : 1;
    const autoRows = Math.max(1, Math.ceil(autos.length / autoCols));
    const autoCardW = (rightW - panelPad * 2 - gap * (autoCols - 1)) / autoCols;
    const autoCardH = 68 * fs;
    const autosBodyH = autos.length
      ? autoRows * autoCardH + (autoRows - 1) * gap : 26 * fs;

    const actionsMinH = panelPad * 2 + headH + gap + actionsBodyH;
    const funcsPanelH = panelPad * 2 + headH + gap + funcsBodyH;
    const autosMinH = panelPad * 2 + headH + gap + autosBodyH;
    const totalH = Math.max(actionsMinH, funcsPanelH + rowGap + autosMinH, 160 * fs);
    const actionsH = totalH;
    const autosTop = layerTop + funcsPanelH + rowGap;
    const autosPanelH = totalH - funcsPanelH - rowGap;

    const headBase = layerTop + panelPad + 21 * fs;
    const bodyTop = layerTop + panelPad + headH + gap;
    const rightLeft = panelLeft + actionsW + panelGap;
    const autoHeadBase = autosTop + panelPad + 21 * fs;
    const autoBodyTop = autosTop + panelPad + headH + gap;

    layerParts += panelBox(panelLeft, layerTop, actionsW, actionsH, 'rgba(245,165,36,.30)') +
      panelHead(panelLeft, headBase,
        '🎯 业务动作层 · GOVERNED ACTIONS · ' + actions.length, '#ffd79a');

    if (!actions.length) {
      layerParts += '<text x="' + (panelLeft + panelPad) + '" y="' + (bodyTop + 16 * fs) +
        '" fill="#6e829c" font-size="' + (10 * fs) + '">暂无业务动作</text>';
    }
    actions.forEach((action, index) => {
      const col = index % actionCols;
      const row = Math.floor(index / actionCols);
      const x = panelLeft + panelPad + col * (actionCardW + gap);
      const y = bodyTop + row * (actionCardH + gap);
      const innerW = actionCardW - 20 * fs;
      const tx = x + 10 * fs;
      const target = targetOf(action);
      layerParts += '<g class="topo-item" data-topo-kind="action" data-topo-id="' +
        esc(action.name) + '"' + (target ? ' data-topo-target="' + esc(target) + '"' : '') + '>' +
        '<rect class="action-node" x="' + x + '" y="' + y + '" width="' + actionCardW +
        '" height="' + actionCardH + '" rx="' + (10 * fs) +
        '" fill="rgba(245,165,36,.10)" stroke="rgba(245,165,36,.65)"/>' +
        '<text x="' + tx + '" y="' + (y + 26 * fs) + '" fill="#ffe3b0" font-size="' +
        (11.5 * fs) + '" font-weight="700">' +
        esc(clipText((action.visibility === 'private' ? '🔒 ' : '🎯 ') + zhAction(action.name), 11.5 * fs, innerW)) + '</text>' +
        '<text x="' + tx + '" y="' + (y + 45 * fs) + '" fill="#c9a06a" font-size="' +
        (8.5 * fs) + '">' + esc(clipText(action.name, 8.5 * fs, innerW)) + '</text>' +
        '<text x="' + tx + '" y="' + (y + 63 * fs) + '" fill="#a98b5f" font-size="' +
        (8 * fs) + '">' +
        esc(clipText((action.visibility === 'private' ? '私有 · 角色 ' : '角色 ') + roleLine(action), 8 * fs, innerW)) + '</text>' +
        (target
          ? '<text x="' + tx + '" y="' + (y + 81 * fs) +
            '" fill="#7fd6c0" font-size="' + (8 * fs) + '">' +
            esc(clipText('作用于 ' + zhObject(target), 8 * fs, innerW)) + '</text>'
          : '') +
        '</g>';
    });

    layerParts += panelBox(rightLeft, layerTop, rightW, funcsPanelH, 'rgba(167,139,250,.30)') +
      panelHead(rightLeft, headBase,
        '🧮 查询与计算函数层 · SEMANTIC FUNCTIONS · ' + funcs.length, '#d8ccff');

    if (!funcs.length) {
      layerParts += '<text x="' + (rightLeft + panelPad) + '" y="' + (bodyTop + 16 * fs) +
        '" fill="#6e829c" font-size="' + (10 * fs) + '">暂无查询函数</text>';
    }
    funcs.forEach((fn, index) => {
      const col = index % funcCols;
      const row = Math.floor(index / funcCols);
      const x = rightLeft + panelPad + col * (funcCardW + gap);
      const y = bodyTop + row * (funcCardH + gap);
      const innerW = funcCardW - 20 * fs;
      const tx = x + 10 * fs;
      const sub = fn.description ? fn.name + ' · ' + fn.description : fn.name;
      layerParts += '<g class="topo-item" data-topo-kind="function" data-topo-id="' + esc(fn.name) + '">' +
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

    layerParts += panelBox(rightLeft, autosTop, rightW, autosPanelH, 'rgba(56,189,248,.35)') +
      panelHead(rightLeft, autoHeadBase,
        '⚡ 自动化规则层 · AUTOMATIONS · ' + autos.length, '#bfe9ff');

    if (!autos.length) {
      layerParts += '<text x="' + (rightLeft + panelPad) + '" y="' + (autoBodyTop + 16 * fs) +
        '" fill="#6e829c" font-size="' + (10 * fs) + '">暂无自动化规则</text>';
    }
    autos.forEach((auto, index) => {
      const col = index % autoCols;
      const row = Math.floor(index / autoCols);
      const x = rightLeft + panelPad + col * (autoCardW + gap);
      const y = autoBodyTop + row * (autoCardH + gap);
      const innerW = autoCardW - 20 * fs;
      const tx = x + 10 * fs;
      const watch = auto.watch_object_type ? ('监听 ' + zhObject(auto.watch_object_type)) : '';
      layerParts += '<g class="topo-item" data-topo-kind="automation" data-topo-id="' + esc(auto.name) + '"' +
        (auto.watch_object_type ? ' data-topo-target="' + esc(auto.watch_object_type) + '"' : '') + '>' +
        '<rect class="auto-node" x="' + x + '" y="' + y + '" width="' + autoCardW +
        '" height="' + autoCardH + '" rx="' + (10 * fs) +
        '" fill="rgba(56,189,248,.10)" stroke="rgba(56,189,248,.65)"/>' +
        '<text x="' + tx + '" y="' + (y + 23 * fs) + '" fill="#d6f2ff" font-size="' +
        (11 * fs) + '" font-weight="700">' +
        esc(clipText('⚡ ' + zh('automations', auto.name, auto.name), 11 * fs, innerW)) +
        '</text>' +
        '<text x="' + tx + '" y="' + (y + 41 * fs) + '" fill="#7f93ab" font-size="' +
        (8.5 * fs) + '">' + esc(clipText(auto.name, 8.5 * fs, innerW)) + '</text>' +
        (watch
          ? '<text x="' + tx + '" y="' + (y + 57 * fs) + '" fill="#7fd6c0" font-size="' +
            (8 * fs) + '">' + esc(clipText(watch, 8 * fs, innerW)) + '</text>'
          : '') +
        '</g>';
    });

    grow(panelLeft, layerTop, panelLeft + panelW, layerTop + totalH);
    nextLayerTop = layerTop + totalH + rowGap;
  }

  if (roleDefs.length) {
    const roleTop = nextLayerTop;
    const roleCols = Math.min(4, Math.max(1, roleDefs.length));
    const roleRows = Math.max(1, Math.ceil(roleDefs.length / roleCols));
    const roleCardW = (panelW - panelPad * 2 - gap * (roleCols - 1)) / roleCols;
    const roleCardH = 88 * fs;
    const roleBodyH = roleRows * roleCardH + (roleRows - 1) * gap;
    const rolePanelH = panelPad * 2 + headH + gap + roleBodyH;
    const roleHeadBase = roleTop + panelPad + 21 * fs;
    const roleBodyTop = roleTop + panelPad + headH + gap;

    layerParts += panelBox(panelLeft, roleTop, panelW, rolePanelH, 'rgba(34,197,94,.32)') +
      panelHead(panelLeft, roleHeadBase,
        '🎭 角色权限层 · ROLES & AUTHORIZATION · ' + roleDefs.length, '#9ef0ba');

    roleDefs.forEach((roleDef, index) => {
      const col = index % roleCols;
      const row = Math.floor(index / roleCols);
      const x = panelLeft + panelPad + col * (roleCardW + gap);
      const y = roleBodyTop + row * (roleCardH + gap);
      const innerW = roleCardW - 20 * fs;
      const tx = x + 10 * fs;
      const allowed = (roleDef.allowed_actions || []).map(zhAction);
      const approvers = (roleDef.approver_actions || []).map(zhAction);
      const allowedText = allowed.length
        ? ('可执行(' + allowed.length + ')：' + allowed.slice(0, 2).join(' / ') +
          (allowed.length > 2 ? ' +' + (allowed.length - 2) : ''))
        : '可执行：无动作';
      const extraText = approvers.length
        ? ('审批：' + approvers.slice(0, 2).join(' / ') + (approvers.length > 2 ? ' +' + (approvers.length - 2) : ''))
        : (roleDef.description || (roleDef.declared ? '显式 RoleDef' : '由动作推断'));
      layerParts += '<g class="topo-item" data-topo-kind="role" data-topo-id="' + esc(roleDef.name) + '">' +
        '<rect class="role-node" x="' + x + '" y="' + y + '" width="' + roleCardW + '" height="' + roleCardH +
        '" rx="' + (10 * fs) + '" fill="rgba(34,197,94,.09)" stroke="rgba(34,197,94,.58)"/>' +
        '<text x="' + tx + '" y="' + (y + 24 * fs) + '" fill="#c7f9d8" font-size="' +
        (11.5 * fs) + '" font-weight="700">' +
        esc(clipText('🎭 ' + zhRole(roleDef.name), 11.5 * fs, innerW)) + '</text>' +
        '<text x="' + tx + '" y="' + (y + 42 * fs) + '" fill="#7bc99a" font-size="' +
        (8.5 * fs) + '">' + esc(clipText(roleDef.name, 8.5 * fs, innerW)) + '</text>' +
        '<text x="' + tx + '" y="' + (y + 59 * fs) + '" fill="#a9e7c0" font-size="' +
        (8 * fs) + '">' + esc(clipText(allowedText, 8 * fs, innerW)) + '</text>' +
        '<text x="' + tx + '" y="' + (y + 76 * fs) + '" fill="#7f9eb5" font-size="' +
        (7.8 * fs) + '">' + esc(clipText(extraText, 7.8 * fs, innerW)) + '</text>' +
        '</g>';
    });
    grow(panelLeft, roleTop, panelLeft + panelW, roleTop + rolePanelH);
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
.topo-item{cursor:pointer}
.topo-item:hover rect{filter:brightness(1.22)}
.topo-item:hover line.edge-vis,.topo-item:hover path.edge-vis{stroke:#7dd3fc;stroke-width:2.6px}
#scene.has-selection .topo-item{opacity:.26;transition:opacity .18s ease,filter .18s ease}
#scene.has-selection .topo-item.is-related{opacity:.82}
#scene.has-selection .topo-item.is-selected{opacity:1;filter:drop-shadow(0 0 12px rgba(56,189,248,.9))}
#scene .topo-item.is-selected rect{stroke:#f8fafc !important;stroke-width:3px !important}
#scene .topo-item.is-selected line.edge-vis,#scene .topo-item.is-selected path.edge-vis{
  stroke:#7dd3fc !important;stroke-width:3px !important}
#scene .topo-item.is-selected text.edge-lbl{fill:#ffffff !important;font-weight:700}
#inspector{position:absolute;top:14px;right:14px;bottom:14px;width:430px;max-width:calc(100vw - 28px);
  z-index:20;display:flex;flex-direction:column;border-radius:14px;
  border:1px solid rgba(56,189,248,.4);background:rgba(9,15,27,.95);
  backdrop-filter:blur(14px);box-shadow:0 20px 54px rgba(0,0,0,.68);overflow:hidden}
#inspector[hidden]{display:none}
.insp-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;
  padding:15px 18px;border-bottom:1px solid var(--line);background:rgba(13,22,38,.92)}
.insp-kind{display:inline-flex;align-items:center;gap:6px;font-size:10.5px;font-weight:700;
  letter-spacing:.7px;padding:3px 9px;border-radius:999px;border:1px solid rgba(56,189,248,.4);
  background:rgba(56,189,248,.12);color:#bae6fd;margin-bottom:7px}
.insp-kind.act{border-color:rgba(245,165,36,.45);background:rgba(245,165,36,.12);color:#fde68a}
.insp-kind.fn{border-color:rgba(167,139,250,.45);background:rgba(167,139,250,.12);color:#e9d5ff}
.insp-kind.auto{border-color:rgba(56,189,248,.45);background:rgba(56,189,248,.12);color:#bae6fd}
.insp-kind.role{border-color:rgba(34,197,94,.45);background:rgba(34,197,94,.12);color:#bbf7d0}
.insp-title{font-size:17px;font-weight:800;color:#f1f5f9;line-height:1.35}
.insp-en{font-family:"SF Mono",Menlo,monospace;font-size:11.5px;color:#7f93ab;margin-top:2px;word-break:break-all}
.insp-close{flex-shrink:0;width:30px;height:30px;padding:0;display:grid;place-items:center;
  font-size:16px;line-height:1;border-radius:8px;border:1px solid var(--line);
  background:rgba(255,255,255,.04);color:#cbd5e1}
.insp-close:hover{background:rgba(239,68,68,.2);border-color:rgba(239,68,68,.5);color:#fecaca}
.insp-body{flex:1;overflow-y:auto;padding:16px 18px;display:flex;flex-direction:column;gap:14px}
.insp-desc{font-size:12.5px;line-height:1.65;color:#cbd5e1;padding:10px 12px;border-radius:10px;
  border:1px solid var(--line);background:rgba(255,255,255,.025)}
.insp-sec-title{font-size:11px;font-weight:700;letter-spacing:.7px;color:#93c5fd;
  text-transform:uppercase;margin-bottom:8px;display:flex;align-items:center;justify-content:space-between}
.insp-table{width:100%;border-collapse:collapse;font-size:12px;border:1px solid var(--line);
  border-radius:10px;overflow:hidden}
.insp-table th,.insp-table td{padding:8px 10px;text-align:left;vertical-align:top;
  border-bottom:1px solid rgba(120,160,210,.12);line-height:1.5}
.insp-table th{background:rgba(56,189,248,.09);color:#bae6fd;font-size:11px;font-weight:700}
.insp-table tr:last-child td{border-bottom:none}
.insp-table tr:nth-child(even) td{background:rgba(255,255,255,.018)}
.insp-name{font-weight:700;color:#e2e8f0}
.insp-code{font-family:"SF Mono",Menlo,monospace;font-size:10.5px;color:#7dd3fc;display:block;margin-top:2px}
.insp-type{font-family:"SF Mono",Menlo,monospace;font-size:11px;color:#a5b4fc;word-break:break-word}
.insp-tags{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.insp-tag{font-size:10px;padding:1px 6px;border-radius:999px;border:1px solid rgba(148,163,184,.28);
  background:rgba(148,163,184,.1);color:#cbd5e1}
.insp-tag.c{border-color:rgba(56,189,248,.45);background:rgba(56,189,248,.13);color:#bae6fd}
.insp-tag.a{border-color:rgba(245,165,36,.45);background:rgba(245,165,36,.13);color:#fde68a}
.insp-tag.r{border-color:rgba(239,68,68,.5);background:rgba(239,68,68,.15);color:#fecaca}
.insp-tag.g{border-color:rgba(34,197,94,.45);background:rgba(34,197,94,.13);color:#bbf7d0}
.insp-chips{display:flex;flex-wrap:wrap;gap:6px}
.rel-chip{font-size:11.5px;padding:5px 10px;border-radius:8px;cursor:pointer;
  border:1px solid rgba(56,189,248,.35);background:rgba(56,189,248,.1);color:#d6f2ff}
.rel-chip:hover{background:rgba(56,189,248,.22);border-color:rgba(56,189,248,.7)}
.insp-empty{font-size:12px;color:var(--dim);padding:8px 0}
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
  <aside id="inspector" hidden aria-label="要素详情">
    <div class="insp-head" id="inspectorHead"></div>
    <div class="insp-body" id="inspectorBody"></div>
  </aside>
  <div id="empty" hidden>该本体暂无对象类型</div>
  <div id="error" hidden>
    <div class="eicon">⚠</div>
    <b id="errorTitle">未找到该本体蓝图</b>
    <p id="errorText"></p>
    <a href="/" target="_self" rel="noopener">返回指挥台</a>
  </div>
  <div class="foot">点击要素查看字段/参数详情 · 拖拽平移 · 滚轮缩放 · 双击适应窗口 · Esc 关闭详情</div>
</main>
<script>
__UI_BUNDLE__
__TOPOLOGY_JS__
const params = new URLSearchParams(location.search);
// ``model`` 是 ``ontology`` 的等价新别名；服务端已拒绝二者冲突的请求。
const ontologyId = params.get('ontology') || params.get('model') || '';
const canvas = document.getElementById('canvas');
const sceneG = document.getElementById('scene');
const pctEl = document.getElementById('zoomPct');
const inspectorEl = document.getElementById('inspector');
const inspectorHeadEl = document.getElementById('inspectorHead');
const inspectorBodyEl = document.getElementById('inspectorBody');
let topoData = null;
let selectedItem = null;
let suppressNextClick = false;
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
          tx: view.tx, ty: view.ty, moved: false, captured: false};
});
canvas.addEventListener('pointermove', event => {
  if (!drag || event.pointerId !== drag.id) return;
  const dx = event.clientX - drag.x;
  const dy = event.clientY - drag.y;
  if (!drag.moved && Math.hypot(dx, dy) > 4) {
    drag.moved = true;
    drag.captured = true;
    try { canvas.setPointerCapture(event.pointerId); } catch (err) {}
    canvas.classList.add('dragging');
  }
  if (!drag.moved) return;
  view.tx = drag.tx + dx;
  view.ty = drag.ty + dy;
  userMoved = true;
  applyView();
});
function endDrag(event) {
  if (!drag || event.pointerId !== drag.id) return;
  const wasMoved = drag.moved;
  const wasCaptured = drag.captured;
  drag = null;
  canvas.classList.remove('dragging');
  if (wasCaptured) {
    try { canvas.releasePointerCapture(event.pointerId); } catch (err) {}
  }
  if (wasMoved) {
    suppressNextClick = true;
    setTimeout(() => { suppressNextClick = false; }, 0);
  }
}
canvas.addEventListener('pointerup', endDrag);
canvas.addEventListener('pointercancel', endDrag);
canvas.addEventListener('lostpointercapture', endDrag);
canvas.addEventListener('click', event => {
  if (suppressNextClick) { suppressNextClick = false; return; }
  const target = event.target && event.target.closest ? event.target.closest('.topo-item') : null;
  if (target) {
    selectTopoItem(target.getAttribute('data-topo-kind'), target.getAttribute('data-topo-id'));
  } else {
    clearSelection();
  }
});
canvas.addEventListener('dblclick', fit);

window.addEventListener('resize', () => {
  if (!scene) return;
  if (userMoved) applyView(); else fit();
});
window.addEventListener('keydown', event => {
  if (event.key === '+' || event.key === '=') zoomCenter(1.2);
  else if (event.key === '-') zoomCenter(1 / 1.2);
  else if (event.key === '0') fit();
  else if (event.key === 'Escape') clearSelection();
});

function actionTargetType(action) {
  if (!topoData || !action) return '';
  const pkToType = {};
  (topoData.object_types || []).forEach(t => { if (t && t.primary_key) pkToType[t.primary_key] = t.name; });
  const props = (action.params && action.params.properties) || {};
  const keys = Object.keys(props);
  for (let i = 0; i < keys.length; i++) {
    if (pkToType[keys[i]]) return pkToType[keys[i]];
  }
  for (let i = 0; i < keys.length; i++) {
    const p = props[keys[i]] || {};
    if (p.property_kind === 'object' && p.range) return p.range;
    if (Array.isArray(p.anyOf)) {
      const ref = p.anyOf.find(a => a && a.property_kind === 'object' && a.range);
      if (ref) return ref.range;
    }
  }
  return '';
}

function clearSelection() {
  selectedItem = null;
  sceneG.classList.remove('has-selection');
  sceneG.querySelectorAll('.topo-item.is-selected,.topo-item.is-related').forEach(el => {
    el.classList.remove('is-selected', 'is-related');
  });
  inspectorEl.hidden = true;
}

function applySelectionHighlight(kind, id) {
  sceneG.querySelectorAll('.topo-item.is-selected,.topo-item.is-related').forEach(el => {
    el.classList.remove('is-selected', 'is-related');
  });
  sceneG.classList.add('has-selection');
  sceneG.querySelectorAll('.topo-item').forEach(el => {
    const ek = el.getAttribute('data-topo-kind');
    const eid = el.getAttribute('data-topo-id');
    if (ek === kind && eid === String(id)) {
      el.classList.add('is-selected');
      return;
    }
    if (kind === 'object_type') {
      const dom = el.getAttribute('data-topo-domain');
      const rng = el.getAttribute('data-topo-range');
      const tgt = el.getAttribute('data-topo-target');
      if (dom === id || rng === id || tgt === id) el.classList.add('is-related');
    } else if (kind === 'object_property' && topoData) {
      const prop = (topoData.object_properties || [])[Number(id)];
      if (prop && ek === 'object_type' && (eid === prop.domain || eid === prop.range)) {
        el.classList.add('is-related');
      }
    } else if ((kind === 'action' || kind === 'automation') && topoData) {
      const selEl = sceneG.querySelector('.topo-item[data-topo-kind="' + kind + '"][data-topo-id="' + CSS.escape(String(id)) + '"]');
      const tgt = selEl && selEl.getAttribute('data-topo-target');
      if (tgt && ek === 'object_type' && eid === tgt) el.classList.add('is-related');
    }
  });
}

function schemaTypeText(prop) {
  if (!prop || typeof prop !== 'object') return 'any';
  if (prop.property_kind === 'object' && prop.range) {
    return zhObject(prop.range) + (prop.multiple ? '[]' : '') + ' (' + prop.range + ')';
  }
  if (Array.isArray(prop.anyOf) && prop.anyOf.length) {
    const parts = prop.anyOf.map(b => schemaTypeText(b)).filter(Boolean);
    return Array.from(new Set(parts)).join(' | ');
  }
  if (prop.type === 'array' && prop.items) {
    return 'array<' + schemaTypeText(prop.items) + '>';
  }
  if (prop.$ref) {
    const parts = String(prop.$ref).split('/');
    return parts[parts.length - 1];
  }
  return prop.type || 'any';
}

function relChip(kind, id, label) {
  return '<button type="button" class="rel-chip" data-jump-kind="' + esc(kind) +
    '" data-jump-id="' + esc(id) + '">' + esc(label) + '</button>';
}

function renderInspectorHeader(kindClass, kindLabel, title, enTitle) {
  inspectorHeadEl.innerHTML =
    '<div><div class="insp-kind ' + esc(kindClass) + '">' + esc(kindLabel) + '</div>' +
    '<div class="insp-title">' + esc(title) + '</div>' +
    (enTitle ? '<div class="insp-en">' + esc(enTitle) + '</div>' : '') +
    '</div>' +
    '<button type="button" class="insp-close" id="inspectorClose" title="关闭详情 (Esc)">×</button>';
  document.getElementById('inspectorClose').addEventListener('click', clearSelection);
}

function renderObjectTypeInspector(t) {
  renderInspectorHeader('', '📦 对象类型 · OBJECT TYPE', zhObject(t.name), t.name);
  const parts = [];
  const metaTags = [
    '<span class="insp-tag ' + (t.owned ? 'a' : 'c') + '">' + (t.owned ? '本体自有' : '来自外部数据源') + '</span>',
    t.primary_key ? '<span class="insp-tag c">主键：' + esc(t.primary_key) + '</span>' : '',
    t.aggregate_root ? '<span class="insp-tag g">聚合根</span>' : '',
    t.read_only_source ? '<span class="insp-tag">只读源</span>' : ''
  ].filter(Boolean).join('');
  parts.push('<div class="insp-tags">' + metaTags + '</div>');

  const fields = t.fields || [];
  if (fields.length) {
    const rows = fields.map(f => {
      const tags = [];
      if (f.name === t.primary_key) tags.push('<span class="insp-tag c">主键</span>');
      if (f.auto_generated) tags.push('<span class="insp-tag">系统生成</span>');
      if (f.property_kind === 'object' && f.range) {
        tags.push('<span class="insp-tag c">对象属性 → ' + esc(zhObject(f.range)) + (f.multiple ? '[]' : '') + '</span>');
      }
      if (f.owned) tags.push('<span class="insp-tag a">本体自有</span>');
      if (f.masked) tags.push('<span class="insp-tag r">🔒 仅可见：' + esc((f.masked_roles || []).map(zhRole).join(' / ')) + '</span>');
      if (Array.isArray(f.choices) && f.choices.length) {
        tags.push('<span class="insp-tag">可选：' + esc(f.choices.map(v => zhValue(f.name, v)).join(' / ')) + '</span>');
      }
      if (f.default !== undefined) {
        tags.push('<span class="insp-tag">默认：' + esc(JSON.stringify(f.default)) + '</span>');
      }
      return '<tr><td><span class="insp-name">' + esc(zhField(t.name, f.name)) + '</span>' +
        '<span class="insp-code">' + esc(f.name) + '</span></td>' +
        '<td><span class="insp-type">' + esc(f.type || 'any') + '</span>' +
        (tags.length ? '<div class="insp-tags">' + tags.join('') + '</div>' : '') +
        '</td></tr>';
    }).join('');
    parts.push('<div><div class="insp-sec-title"><span>字段定义 · FIELDS</span><span>' + fields.length + ' 项</span></div>' +
      '<table class="insp-table"><thead><tr><th>字段</th><th>类型与语义</th></tr></thead><tbody>' + rows + '</tbody></table></div>');
  } else {
    parts.push('<div class="insp-empty">该对象类型暂无声明字段。</div>');
  }

  const relChips = [];
  (topoData.object_properties || []).forEach((l, idx) => {
    if (l.domain === t.name || l.range === t.name) {
      const lname = zh('object_properties', l.name, '') || zh('links', l.name, l.name);
      relChips.push(relChip('object_property', String(idx), '🔗 ' + lname + ' (' + zhObject(l.domain) + ' → ' + zhObject(l.range) + ')'));
    }
  });
  (topoData.actions || []).forEach(a => {
    if (actionTargetType(a) === t.name) {
      relChips.push(relChip('action', a.name, '🎯 ' + zhAction(a.name)));
    }
  });
  (topoData.automations || []).forEach(au => {
    if (au.watch_object_type === t.name) {
      relChips.push(relChip('automation', au.name, '⚡ ' + zh('automations', au.name, au.name)));
    }
  });
  if (relChips.length) {
    parts.push('<div><div class="insp-sec-title"><span>关联要素 · RELATED</span><span>' + relChips.length + ' 项</span></div>' +
      '<div class="insp-chips">' + relChips.join('') + '</div></div>');
  }
  inspectorBodyEl.innerHTML = parts.join('');
}

function renderActionInspector(a) {
  renderInspectorHeader('act', '🎯 业务动作 · GOVERNED ACTION', zhAction(a.name), a.name);
  const parts = [];
  if (a.description) {
    parts.push('<div class="insp-desc">' + esc(a.description) + '</div>');
  }
  const target = actionTargetType(a);
  const props = (a.params && a.params.properties) || {};
  const requiredSet = new Set((a.params && a.params.required) || []);
  const keys = Object.keys(props);
  if (keys.length) {
    const rows = keys.map(k => {
      const p = props[k] || {};
      const isReq = requiredSet.has(k);
      const tags = [
        isReq ? '<span class="insp-tag r">必填</span>' : '<span class="insp-tag">可选</span>'
      ];
      if (p.auto_generated) tags.push('<span class="insp-tag">系统生成</span>');
      if (p.property_kind === 'object' && p.range) {
        tags.push('<span class="insp-tag c">对象引用 → ' + esc(zhObject(p.range)) + (p.multiple ? '[]' : '') + '</span>');
      }
      if (Array.isArray(p.enum) && p.enum.length) {
        tags.push('<span class="insp-tag">枚举：' + esc(p.enum.join(' / ')) + '</span>');
      }
      if (p.default !== undefined) {
        tags.push('<span class="insp-tag">默认：' + esc(JSON.stringify(p.default)) + '</span>');
      }
      return '<tr><td><span class="insp-name">' + esc(zhField(target || null, k)) + '</span>' +
        '<span class="insp-code">' + esc(k) + '</span></td>' +
        '<td><span class="insp-type">' + esc(schemaTypeText(p)) + '</span>' +
        '<div class="insp-tags">' + tags.join('') + '</div>' +
        (p.description ? '<div style="font-size:11px;color:#94a3b8;margin-top:4px">' + esc(p.description) + '</div>' : '') +
        '</td></tr>';
    }).join('');
    parts.push('<div><div class="insp-sec-title"><span>输入参数 · PARAMETERS</span><span>' + keys.length + ' 项</span></div>' +
      '<table class="insp-table"><thead><tr><th>参数</th><th>类型与约束</th></tr></thead><tbody>' + rows + '</tbody></table></div>');
  } else {
    parts.push('<div class="insp-empty">该动作无需输入参数。</div>');
  }

  const govRows = [
    '<tr><td class="insp-name">可执行角色</td><td>' + esc((a.allowed_roles || []).map(zhRole).join(' / ') || '无限制') + '</td></tr>',
    '<tr><td class="insp-name">审批角色</td><td>' + esc((a.approver_roles || []).map(zhRole).join(' / ') || '无需审批') + '</td></tr>',
    '<tr><td class="insp-name">可见性 / 归属</td><td>' + (a.visibility === 'private' ? '🔒 私有（仅内部触发）' : '公开动作') + ' · ' + (a.affects_source ? '来自外部数据源' : '本体自有') + '</td></tr>'
  ].join('');
  parts.push('<div><div class="insp-sec-title"><span>治理与权限 · GOVERNANCE</span></div>' +
    '<table class="insp-table"><tbody>' + govRows + '</tbody></table></div>');

  const relChips = [];
  if (target) relChips.push(relChip('object_type', target, '📦 作用对象：' + zhObject(target)));
  (a.allowed_roles || []).forEach(r => relChips.push(relChip('role', r, '🎭 ' + zhRole(r))));
  if (relChips.length) {
    parts.push('<div><div class="insp-sec-title"><span>关联要素 · RELATED</span></div>' +
      '<div class="insp-chips">' + relChips.join('') + '</div></div>');
  }
  inspectorBodyEl.innerHTML = parts.join('');
}

function renderPropertyInspector(l) {
  const lname = zh('object_properties', l.name, '') || zh('links', l.name, l.name);
  renderInspectorHeader('', '🔗 对象属性 · OBJECT PROPERTY', lname, l.name);
  const parts = [];
  if (l.description) parts.push('<div class="insp-desc">' + esc(l.description) + '</div>');
  const rows = [
    '<tr><td class="insp-name">起始对象 (Domain)</td><td>' + esc(zhObject(l.domain)) + ' <span class="insp-code">' + esc(l.domain) + '</span></td></tr>',
    '<tr><td class="insp-name">目标对象 (Range)</td><td>' + esc(zhObject(l.range)) + ' <span class="insp-code">' + esc(l.range) + '</span></td></tr>',
    '<tr><td class="insp-name">基数 (Cardinality)</td><td>' + esc(zhCardinality(l.cardinality)) + '</td></tr>',
    l.field ? '<tr><td class="insp-name">关联字段</td><td><span class="insp-code">' + esc(l.field) + '</span></td></tr>' : '',
    '<tr><td class="insp-name">主权归属</td><td>' + (l.owned ? '本体自有' : '来自外部数据源') + '</td></tr>'
  ].filter(Boolean).join('');
  parts.push('<table class="insp-table"><tbody>' + rows + '</tbody></table>');
  parts.push('<div><div class="insp-sec-title"><span>两端对象 · ENDPOINTS</span></div><div class="insp-chips">' +
    relChip('object_type', l.domain, '📦 ' + zhObject(l.domain)) +
    relChip('object_type', l.range, '📦 ' + zhObject(l.range)) +
    '</div></div>');
  inspectorBodyEl.innerHTML = parts.join('');
}

function renderFunctionInspector(fn) {
  renderInspectorHeader('fn', '🧮 查询函数 · SEMANTIC FUNCTION', zh('functions', fn.name, fn.name), fn.name);
  const parts = [];
  parts.push(fn.description
    ? '<div class="insp-desc">' + esc(fn.description) + '</div>'
    : '<div class="insp-empty">暂无函数说明。</div>');
  inspectorBodyEl.innerHTML = parts.join('');
}

function renderAutomationInspector(auto) {
  renderInspectorHeader('auto', '⚡ 自动化规则 · AUTOMATION', zh('automations', auto.name, auto.name), auto.name);
  const parts = [];
  if (auto.description) parts.push('<div class="insp-desc">' + esc(auto.description) + '</div>');
  if (auto.watch_object_type) {
    parts.push('<div><div class="insp-sec-title"><span>监听对象 · WATCH TARGET</span></div><div class="insp-chips">' +
      relChip('object_type', auto.watch_object_type, '📦 ' + zhObject(auto.watch_object_type)) + '</div></div>');
  }
  inspectorBodyEl.innerHTML = parts.join('');
}

function renderRoleInspector(r) {
  renderInspectorHeader('role', '🎭 角色权限 · ROLE', zhRole(r.name), r.name);
  const parts = [];
  if (r.description) parts.push('<div class="insp-desc">' + esc(r.description) + '</div>');
  parts.push('<div class="insp-tags"><span class="insp-tag ' + (r.declared ? 'g' : '') + '">' +
    (r.declared ? '显式 RoleDef' : '由动作推断') + '</span></div>');
  if ((r.allowed_actions || []).length) {
    parts.push('<div><div class="insp-sec-title"><span>可执行动作 · ALLOWED ACTIONS</span><span>' + r.allowed_actions.length + ' 项</span></div>' +
      '<div class="insp-chips">' + r.allowed_actions.map(a => relChip('action', a, '🎯 ' + zhAction(a))).join('') + '</div></div>');
  }
  if ((r.approver_actions || []).length) {
    parts.push('<div><div class="insp-sec-title"><span>可审批动作 · APPROVER</span><span>' + r.approver_actions.length + ' 项</span></div>' +
      '<div class="insp-chips">' + r.approver_actions.map(a => relChip('action', a, '✅ ' + zhAction(a))).join('') + '</div></div>');
  }
  if ((r.masked_fields || []).length) {
    parts.push('<div><div class="insp-sec-title"><span>可见敏感字段 · MASKED FIELDS</span></div>' +
      '<div class="insp-tags">' + r.masked_fields.map(f => '<span class="insp-tag r">🔒 ' + esc(f) + '</span>').join('') + '</div></div>');
  }
  inspectorBodyEl.innerHTML = parts.join('');
}

function selectTopoItem(kind, id) {
  if (!topoData || !kind) return;
  let handled = false;
  if (kind === 'object_type') {
    const t = (topoData.object_types || []).find(item => item.name === id);
    if (t) { renderObjectTypeInspector(t); handled = true; }
  } else if (kind === 'action') {
    const a = (topoData.actions || []).find(item => item.name === id);
    if (a) { renderActionInspector(a); handled = true; }
  } else if (kind === 'object_property') {
    const l = (topoData.object_properties || [])[Number(id)];
    if (l) { renderPropertyInspector(l); handled = true; }
  } else if (kind === 'function') {
    const fn = (topoData.functions || []).find(item => item.name === id);
    if (fn) { renderFunctionInspector(fn); handled = true; }
  } else if (kind === 'automation') {
    const auto = (topoData.automations || []).find(item => item.name === id);
    if (auto) { renderAutomationInspector(auto); handled = true; }
  } else if (kind === 'role') {
    const r = (topoData.role_defs || []).find(item => item.name === id);
    if (r) { renderRoleInspector(r); handled = true; }
  }
  if (!handled) return;
  selectedItem = {kind, id};
  applySelectionHighlight(kind, id);
  inspectorEl.hidden = false;
}

inspectorBodyEl.addEventListener('click', event => {
  const btn = event.target && event.target.closest ? event.target.closest('[data-jump-kind]') : null;
  if (!btn) return;
  selectTopoItem(btn.getAttribute('data-jump-kind'), btn.getAttribute('data-jump-id'));
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
    showError('缺少模型参数', '请从指挥台点击“查看大图”，或使用 ?ontology=<模型编号>（等价 ?model=<模型编号>）访问。');
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
    topoData = data;
    clearSelection();
    const name = zh('ontologies', data.ontology.ontology_id, data.ontology.name);
    const category = zh('categories', data.ontology.category, data.ontology.category);
    document.getElementById('title').textContent = '本体关系图 · ' + name;
    document.title = '本体关系图 · ' + name;
    document.getElementById('subtitle').textContent =
      category + ' · ' + (data.object_types || []).length + ' 个对象类型 · ' +
      (data.object_properties || []).length + ' 个对象属性 · ' +
      (data.actions || []).length + ' 个业务动作 · ' +
      (data.functions || []).length + ' 个查询函数 · ' +
      (data.automations || []).length + ' 个自动化规则 · ' +
      (data.role_defs || []).length + ' 个角色';
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


DOCS_PAGE_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>架构文档中心 · __TITLE__</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#05080f;--panel:#0c1420;--line:rgba(120,160,210,.2);
  --txt:#e6eefb;--dim:#8ea3bd;--cy:#38bdf8;--em:#34d399;--am:#f59e0b}
html,body{min-height:100%}
body{background:
  radial-gradient(1200px 600px at 8% -10%,rgba(56,189,248,.14),transparent 62%),
  radial-gradient(900px 520px at 98% -6%,rgba(129,140,248,.12),transparent 60%),
  linear-gradient(#05080f,#02040a);
  color:var(--txt);font-family:"PingFang SC","Hiragino Sans GB","Heiti SC",sans-serif;
  display:flex;flex-direction:column}
header{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  padding:12px 20px;border-bottom:1px solid var(--line);background:rgba(8,14,22,.94);
  backdrop-filter:blur(10px)}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.mark{width:36px;height:36px;border-radius:10px;display:grid;place-items:center;
  font-size:18px;font-weight:800;color:#03121d;flex-shrink:0;
  background:linear-gradient(135deg,#38bdf8,#6366f1);box-shadow:0 8px 22px rgba(56,189,248,.35)}
.brand b{font-size:15px;letter-spacing:.3px;display:block}
.brand span{display:block;font-size:11px;color:var(--dim);margin-top:2px;font-family:"SF Mono",Menlo,monospace}
.docnav{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-left:auto}
.doc-tab{font-size:11.5px;padding:6px 11px;border-radius:8px;cursor:pointer;
  border:1px solid var(--line);background:rgba(255,255,255,.03);color:#b9cbe0;white-space:nowrap}
.doc-tab:hover{background:rgba(56,189,248,.12);color:#e0f2fe}
.doc-tab.on{color:#eaf8ff;border-color:rgba(56,189,248,.6);
  background:linear-gradient(135deg,rgba(56,189,248,.24),rgba(129,140,248,.2))}
a.back{font-size:11.5px;padding:6px 12px;border-radius:8px;text-decoration:none;white-space:nowrap;
  border:1px solid rgba(167,139,250,.45);background:rgba(167,139,250,.14);color:#e3dbff}
a.back:hover{background:rgba(167,139,250,.26)}
main{max-width:1040px;width:100%;margin:0 auto;padding:20px 20px 60px}
.doc-card{background:linear-gradient(165deg,rgba(15,26,42,.92),rgba(7,12,21,.96));
  border:1px solid var(--line);border-radius:16px;padding:28px 32px;
  box-shadow:0 18px 46px rgba(0,0,0,.45)}
#docStatus{font-size:12px;color:var(--dim);padding:16px 0}
#docStatus.err{color:#fca5a5}
.md h1{font-size:24px;line-height:1.35;margin:0 0 14px;padding-bottom:10px;border-bottom:1px solid var(--line);color:#f1f5f9}
.md h2{font-size:18px;line-height:1.4;margin:26px 0 10px;padding-bottom:6px;border-bottom:1px dashed var(--line);color:#bae6fd}
.md h3{font-size:15px;line-height:1.45;margin:18px 0 8px;color:#e0f2fe}
.md h4{font-size:13.5px;margin:14px 0 6px;color:#cbd5e1}
.md p{font-size:13.5px;line-height:1.8;color:#cbd5e1;margin:8px 0}
.md ul,.md ol{margin:8px 0 12px 22px;color:#cbd5e1;font-size:13.5px;line-height:1.75}
.md li{margin:4px 0}
.md hr{border:none;border-top:1px solid var(--line);margin:20px 0}
.md blockquote{margin:12px 0;padding:10px 14px;border-left:3px solid var(--cy);
  background:rgba(56,189,248,.07);border-radius:0 10px 10px 0;color:#d6e6f5;font-size:13px;line-height:1.7}
.md code{font-family:"SF Mono",Menlo,monospace;font-size:12px;background:rgba(15,23,42,.85);
  border:1px solid rgba(148,163,184,.2);padding:1px 5px;border-radius:5px;color:#7dd3fc}
.md pre{margin:12px 0;padding:12px 14px;background:rgba(3,7,14,.85);border:1px solid var(--line);
  border-radius:10px;overflow-x:auto;font-family:"SF Mono",Menlo,monospace;font-size:12px;line-height:1.55;color:#cbd5e1}
.md pre code{background:none;border:none;padding:0;color:inherit}
.md table{width:100%;border-collapse:collapse;margin:14px 0;font-size:12.5px}
.md th,.md td{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top;line-height:1.6}
.md th{background:rgba(56,189,248,.1);color:#bae6fd;font-weight:700}
.md tr:nth-child(even) td{background:rgba(255,255,255,.02)}
.md a{color:#38bdf8;text-decoration:none;border-bottom:1px solid rgba(56,189,248,.35)}
.md a:hover{color:#7dd3fc;border-bottom-color:#7dd3fc}
.mermaid-card{margin:14px 0;padding:14px 16px;border-radius:12px;border:1px solid rgba(56,189,248,.28);
  background:rgba(5,12,24,.78)}
.mermaid-hdr{font-size:11px;font-weight:700;letter-spacing:.6px;color:#7dd3fc;margin-bottom:10px;display:flex;justify-content:space-between}
.mermaid-groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-bottom:10px}
.mermaid-grp{border:1px solid var(--line);border-radius:10px;padding:10px;background:rgba(15,23,42,.6)}
.mermaid-grp h5{font-size:11.5px;color:#a5e4ff;margin-bottom:7px;font-weight:700}
.mermaid-node{display:inline-block;margin:3px 4px 3px 0;padding:4px 8px;border-radius:7px;font-size:11.5px;
  border:1px solid rgba(52,211,153,.35);background:rgba(16,185,129,.1);color:#d1fae5}
.mermaid-edges{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.mermaid-edge{font-size:11px;font-family:"SF Mono",Menlo,monospace;color:#cbd5e1;padding:3px 8px;
  border-radius:6px;border:1px solid rgba(148,163,184,.2);background:rgba(255,255,255,.03)}
</style></head>
<body>
<header>
  <div class="brand">
    <div class="mark">◈</div>
    <div><b id="docTitle">架构文档中心</b><span id="docPath">docs/ARCHITECTURE.md</span></div>
  </div>
  <nav class="docnav" id="docNav">
    <a class="back" id="back" href="/" target="_self" rel="noopener">返回指挥台</a>
  </nav>
</header>
<main>
  <div class="doc-card">
    <div id="docStatus">正在加载架构文档…</div>
    <article id="docArticle" class="md"></article>
  </div>
</main>
<script>
const DOCS_CATALOG = __DOCS_CATALOG_JSON__;
const DOC_MAP = {};
(function initDocNav() {
  const nav = document.getElementById('docNav');
  const backLink = document.getElementById('back');
  for (const item of DOCS_CATALOG) {
    if (item && item.basename && item.key) {
      DOC_MAP[item.basename] = item.key;
    }
    const btn = document.createElement('button');
    btn.className = 'doc-tab';
    btn.dataset.doc = item.key;
    btn.textContent = item.tab_label || item.title || item.key;
    btn.addEventListener('click', function() { selectDoc(item.key); });
    nav.insertBefore(btn, backLink);
  }
})();

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function renderInline(text) {
  let out = '';
  const codeParts = String(text || '').split(/(`[^`]+`)/g);
  for (const part of codeParts) {
    if (part.startsWith('`') && part.endsWith('`') && part.length >= 2) {
      out += '<code>' + esc(part.slice(1, -1)) + '</code>';
      continue;
    }
    let chunk = esc(part);
    chunk = chunk.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function(_, label, href) {
      const cleanHref = href.split('#')[0];
      const base = cleanHref.split('/').pop();
      if (DOC_MAP[base]) {
        const key = DOC_MAP[base];
        return '<a href="/docs?doc=' + encodeURIComponent(key) + '" onclick="event.preventDefault();selectDoc(\'' + key + '\')">' + label + '</a>';
      }
      if (/^https?:\/\//i.test(href)) {
        return '<a href="' + href + '" target="_blank" rel="noopener">' + label + '</a>';
      }
      return '<a href="' + href + '">' + label + '</a>';
    });
    chunk = chunk.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    chunk = chunk.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    out += chunk;
  }
  return out;
}

function renderMermaidBlock(src) {
  const lines = src.split('\n').map(l => l.trim()).filter(l => l && !l.startsWith('%%'));
  const groups = [];
  let curGroup = { name: '结构节点', nodes: {} };
  const allNodes = {};
  const edges = [];

  function recordNode(raw) {
    const m = raw.match(/^([A-Za-z0-9_.-]+)(?:\s*[\[\(\{]+"?([^\[\]\(\)\{\}"]+?)"?[\]\)\}]+)?/);
    if (!m) return raw.trim();
    const id = m[1];
    const label = m[2] ? m[2].replace(/<br\s*\/?>/gi, ' ') : (allNodes[id] || id);
    allNodes[id] = label;
    if (!curGroup.nodes[id]) curGroup.nodes[id] = label;
    return label;
  }

  for (const line of lines) {
    if (/^(flowchart|graph|sequenceDiagram|stateDiagram)/i.test(line)) continue;
    const sub = line.match(/^subgraph\s+(?:[^\[]*\[)?([^\]]+)\]?/i);
    if (sub) {
      if (Object.keys(curGroup.nodes).length > 0) groups.push(curGroup);
      curGroup = { name: sub[1].replace(/^["']|["']$/g, '').trim(), nodes: {} };
      continue;
    }
    if (/^end$/i.test(line)) {
      if (Object.keys(curGroup.nodes).length > 0) groups.push(curGroup);
      curGroup = { name: '结构节点', nodes: {} };
      continue;
    }
    const edgeMatch = line.match(/^(.+?)\s*(?:--+>?|-\.->|==>)\s*(?:\|([^|]+)\|)?\s*(.+)$/);
    if (edgeMatch) {
      const fromLbl = recordNode(edgeMatch[1].trim());
      const toLbl = recordNode(edgeMatch[3].trim());
      const edgeLbl = edgeMatch[2] ? ' [' + edgeMatch[2].trim() + ']' : '';
      edges.push(fromLbl + ' → ' + toLbl + edgeLbl);
      continue;
    }
    if (/^[A-Za-z0-9_.-]+\s*[\[\(\{]/.test(line)) recordNode(line);
  }
  if (Object.keys(curGroup.nodes).length > 0) groups.push(curGroup);
  if (groups.length === 0 && edges.length === 0) {
    return '<pre><code>' + esc(src) + '</code></pre>';
  }
  let html = '<div class="mermaid-card"><div class="mermaid-hdr"><span>◈ MERMAID 架构图视图</span><span>结构化拓扑解析</span></div>';
  if (groups.length) {
    html += '<div class="mermaid-groups">';
    for (const g of groups) {
      html += '<div class="mermaid-grp"><h5>' + esc(g.name) + '</h5>';
      for (const lbl of Object.values(g.nodes)) {
        html += '<span class="mermaid-node">' + esc(lbl) + '</span>';
      }
      html += '</div>';
    }
    html += '</div>';
  }
  if (edges.length) {
    html += '<div class="mermaid-edges">' + edges.map(e => '<span class="mermaid-edge">' + esc(e) + '</span>').join('') + '</div>';
  }
  html += '</div>';
  return html;
}

function renderMarkdown(md) {
  const lines = String(md || '').replace(/\r\n/g, '\n').split('\n');
  let html = '';
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line.trim())) {
      const lang = line.trim().slice(3).trim().toLowerCase();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) {
        buf.push(lines[i]);
        i++;
      }
      i++;
      const codeText = buf.join('\n');
      if (lang === 'mermaid') {
        html += renderMermaidBlock(codeText);
      } else {
        html += '<pre><code>' + esc(codeText) + '</code></pre>';
      }
      continue;
    }
    if (!line.trim()) { i++; continue; }
    if (/^(?:---|\*\*\*|___)\s*$/.test(line.trim())) { html += '<hr>'; i++; continue; }
    const h = line.match(/^(#{1,4})\s+(.+)$/);
    if (h) {
      const lvl = h[1].length;
      html += '<h' + lvl + '>' + renderInline(h[2].trim()) + '</h' + lvl + '>';
      i++;
      continue;
    }
    if (line.trim().startsWith('>')) {
      const bq = [];
      while (i < lines.length && lines[i].trim().startsWith('>')) {
        bq.push(lines[i].trim().replace(/^>\s?/, ''));
        i++;
      }
      html += '<blockquote>' + bq.map(renderInline).join('<br>') + '</blockquote>';
      continue;
    }
    if (line.includes('|') && i + 1 < lines.length && /^\s*\|?\s*[:\-| ]+\s*$/.test(lines[i + 1]) && lines[i + 1].includes('-')) {
      const parseCells = row => row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
      const headers = parseCells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(parseCells(lines[i]));
        i++;
      }
      html += '<table><thead><tr>' + headers.map(c => '<th>' + renderInline(c) + '</th>').join('') + '</tr></thead><tbody>';
      for (const r of rows) {
        html += '<tr>' + headers.map((_, idx) => '<td>' + renderInline(r[idx] || '') + '</td>').join('') + '</tr>';
      }
      html += '</tbody></table>';
      continue;
    }
    if (/^\s*[-*+]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*+]\s+/, ''));
        i++;
      }
      html += '<ul>' + items.map(it => '<li>' + renderInline(it) + '</li>').join('') + '</ul>';
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ''));
        i++;
      }
      html += '<ol>' + items.map(it => '<li>' + renderInline(it) + '</li>').join('') + '</ol>';
      continue;
    }
    const para = [];
    while (
      i < lines.length && lines[i].trim() &&
      !/^```/.test(lines[i].trim()) && !/^(#{1,4})\s+/.test(lines[i]) &&
      !lines[i].trim().startsWith('>') && !/^\s*[-*+]\s+/.test(lines[i]) &&
      !/^\s*\d+\.\s+/.test(lines[i]) && !/^(?:---|\*\*\*|___)\s*$/.test(lines[i].trim())
    ) {
      para.push(lines[i].trim());
      i++;
    }
    html += '<p>' + renderInline(para.join(' ')) + '</p>';
  }
  return html;
}

function activeDocKey() {
  const p = new URLSearchParams(window.location.search);
  const key = (p.get('doc') || 'architecture').trim();
  return key || 'architecture';
}

async function selectDoc(docKey, pushUrl) {
  const buttons = document.querySelectorAll('.doc-tab');
  for (const btn of buttons) {
    btn.classList.toggle('on', btn.dataset.doc === docKey);
  }
  const statusEl = document.getElementById('docStatus');
  const articleEl = document.getElementById('docArticle');
  statusEl.hidden = false;
  statusEl.className = '';
  statusEl.textContent = '正在加载架构文档…';
  if (pushUrl !== false) {
    const nextUrl = '/docs?doc=' + encodeURIComponent(docKey);
    window.history.replaceState(null, '', nextUrl);
  }
  try {
    const res = await fetch('/api/docs?doc=' + encodeURIComponent(docKey));
    const data = await res.json();
    if (!res.ok || !data.ok) throw new Error(data.error || ('HTTP ' + res.status));
    document.getElementById('docTitle').textContent = data.title || '架构文档';
    document.getElementById('docPath').textContent = data.path || '';
    document.title = (data.title || '架构文档') + ' · OpenOntology';
    statusEl.hidden = true;
    articleEl.innerHTML = renderMarkdown(data.content || '');
  } catch (err) {
    statusEl.hidden = false;
    statusEl.className = 'err';
    statusEl.textContent = '加载文档失败：' + String(err && err.message ? err.message : err);
    articleEl.innerHTML = '';
  }
}

selectDoc(activeDocKey(), false);
</script></body></html>
"""
