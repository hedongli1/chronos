"""Chinese-language web dashboard for live agent visualization."""

from __future__ import annotations

import html
import json
import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)


SnapshotFn = Callable[[], dict[str, Any]]
TraceFn = Callable[[str, str, int], list[dict[str, Any]]]
ControlFn = Callable[[dict[str, Any]], dict[str, Any]]


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #081120;
      --panel: rgba(15, 23, 42, 0.88);
      --panel-strong: rgba(15, 23, 42, 0.98);
      --border: rgba(148, 163, 184, 0.18);
      --text: #e5eefc;
      --muted: #93a4bf;
      --accent: #7c3aed;
      --accent-2: #22c55e;
      --warn: #f59e0b;
      --danger: #ef4444;
      --info: #38bdf8;
      --shadow: 0 24px 80px rgba(2, 6, 23, 0.55);
      --radius: 20px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: Inter, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(124, 58, 237, 0.28), transparent 28%),
        radial-gradient(circle at top right, rgba(56, 189, 248, 0.18), transparent 24%),
        linear-gradient(180deg, #060b16 0%, #081120 100%);
      color: var(--text);
    }}
    .app {{ max-width: 1600px; margin: 0 auto; padding: 28px; }}
    .hero {{
      display: flex; justify-content: space-between; align-items: flex-end; gap: 24px;
      padding: 28px; border: 1px solid var(--border); border-radius: 28px;
      background: linear-gradient(135deg, rgba(15,23,42,.95), rgba(15,23,42,.72));
      box-shadow: var(--shadow); backdrop-filter: blur(16px);
    }}
    .hero h1 {{ margin: 0 0 8px; font-size: 32px; }}
    .hero p {{ margin: 0; color: var(--muted); line-height: 1.6; }}
    .pill {{
      display: inline-flex; align-items: center; gap: 8px; padding: 9px 14px;
      border-radius: 999px; background: rgba(124, 58, 237, 0.14); color: #d8c8ff;
      border: 1px solid rgba(124, 58, 237, 0.22); font-size: 13px;
    }}
    .grid {{ display: grid; gap: 18px; margin-top: 18px; }}
    .metrics {{ grid-template-columns: repeat(6, minmax(0, 1fr)); }}
    .cards {{ grid-template-columns: 1.2fr .8fr; }}
    .main {{ grid-template-columns: 1.15fr .85fr; align-items: start; }}
    .panel {{
      border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel);
      box-shadow: var(--shadow); backdrop-filter: blur(16px); overflow: hidden;
    }}
    .panel-header {{
      display: flex; justify-content: space-between; align-items: center; gap: 16px;
      padding: 18px 20px; border-bottom: 1px solid var(--border); background: rgba(2, 6, 23, 0.18);
    }}
    .panel-header h2 {{ margin: 0; font-size: 18px; }}
    .panel-header small {{ color: var(--muted); }}
    .panel-body {{ padding: 18px 20px; }}
    .metric {{ padding: 18px 20px; }}
    .metric .label {{ color: var(--muted); font-size: 13px; margin-bottom: 10px; }}
    .metric .value {{ font-size: 30px; font-weight: 700; letter-spacing: -0.03em; }}
    .metric .hint {{ margin-top: 8px; color: var(--muted); font-size: 12px; }}
    .status-ok {{ color: #86efac; }}
    .status-warn {{ color: #fcd34d; }}
    .status-bad {{ color: #fca5a5; }}
    .status-info {{ color: #7dd3fc; }}
    .split {{ display: grid; gap: 18px; grid-template-columns: 360px 1fr; align-items: start; }}
    .list {{ display: flex; flex-direction: column; gap: 12px; }}
    .challenge {{
      border: 1px solid rgba(148,163,184,.16); border-radius: 18px; padding: 14px 16px;
      background: rgba(15, 23, 42, 0.72); cursor: pointer; transition: .18s ease;
    }}
    .challenge:hover {{ transform: translateY(-1px); border-color: rgba(56,189,248,.35); }}
    .challenge.active {{ border-color: rgba(124,58,237,.55); box-shadow: inset 0 0 0 1px rgba(124,58,237,.15); }}
    .challenge-top {{ display: flex; justify-content: space-between; gap: 12px; align-items: start; }}
    .challenge h3 {{ margin: 0 0 6px; font-size: 16px; }}
    .challenge .meta {{ color: var(--muted); font-size: 12px; line-height: 1.55; }}
    .badge {{
      padding: 4px 10px; border-radius: 999px; font-size: 12px; border: 1px solid transparent;
      white-space: nowrap;
    }}
    .badge.running {{ background: rgba(56, 189, 248, 0.12); color: #7dd3fc; border-color: rgba(56, 189, 248, 0.24); }}
    .badge.finished {{ background: rgba(148, 163, 184, 0.12); color: #cbd5e1; border-color: rgba(148, 163, 184, 0.24); }}
    .badge.solved {{ background: rgba(34, 197, 94, 0.12); color: #86efac; border-color: rgba(34, 197, 94, 0.24); }}
    .badge.error {{ background: rgba(239, 68, 68, 0.12); color: #fca5a5; border-color: rgba(239, 68, 68, 0.24); }}
    .badge.paused {{ background: rgba(245, 158, 11, 0.12); color: #fcd34d; border-color: rgba(245, 158, 11, 0.24); }}
    .agents {{ display: grid; gap: 12px; margin-top: 14px; }}
    .agent {{
      border: 1px solid rgba(148,163,184,.16); border-radius: 16px; background: rgba(2, 6, 23, 0.22);
      padding: 14px 14px 12px;
    }}
    .agent-top {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; }}
    .agent code, .trace code {{
      display: inline-block; padding: 2px 6px; border-radius: 8px; background: rgba(148,163,184,.12); color: #dbeafe;
    }}
    .findings {{ margin-top: 10px; color: #dbeafe; font-size: 13px; line-height: 1.6; white-space: pre-wrap; }}
    .agent .hint {{ color: var(--muted); margin-top: 6px; font-size: 12px; }}
    .trace-toolbar {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .select, .input {{
      min-width: 220px; border: 1px solid rgba(148,163,184,.18); border-radius: 12px; padding: 10px 12px;
      background: rgba(2, 6, 23, 0.48); color: var(--text);
    }}
    .input {{ min-width: 0; width: 100%; }}
    .trace {{
      border-radius: 16px; border: 1px solid rgba(148,163,184,.16); background: rgba(2, 6, 23, 0.28);
      padding: 14px; min-height: 420px; max-height: 720px; overflow: auto;
    }}
    .trace-line {{
      display: grid; grid-template-columns: 84px 120px 1fr; gap: 12px; align-items: start;
      padding: 10px 0; border-bottom: 1px solid rgba(148,163,184,.08); font-size: 13px; line-height: 1.55;
    }}
    .trace-line:last-child {{ border-bottom: 0; }}
    .trace-time {{ color: var(--muted); }}
    .trace-type {{ color: #7dd3fc; }}
    .trace-text {{ color: #e2e8f0; white-space: pre-wrap; word-break: break-word; }}
    .footer {{ margin-top: 18px; color: var(--muted); font-size: 12px; text-align: center; }}
    .progress {{ height: 10px; border-radius: 999px; background: rgba(148,163,184,.12); overflow: hidden; }}
    .progress > span {{ display: block; height: 100%; background: linear-gradient(90deg, var(--accent), #38bdf8); }}
    .row {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }}
    .empty {{ color: var(--muted); padding: 18px 0; }}
    .controls-grid {{ display: grid; grid-template-columns: 1fr; gap: 14px; }}
    .control-actions {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
    .button {{
      border: 1px solid rgba(148,163,184,.18); border-radius: 12px; padding: 10px 14px; cursor: pointer;
      background: rgba(2, 6, 23, 0.48); color: var(--text);
    }}
    .button.primary {{ background: linear-gradient(90deg, rgba(124,58,237,.9), rgba(56,189,248,.85)); border-color: transparent; }}
    .button.warn {{ background: rgba(245, 158, 11, 0.15); border-color: rgba(245, 158, 11, 0.28); color: #fcd34d; }}
    .button.danger {{ background: rgba(239, 68, 68, 0.14); border-color: rgba(239, 68, 68, 0.28); color: #fca5a5; }}
    @media (max-width: 1280px) {{
      .metrics, .cards, .main, .split {{ grid-template-columns: 1fr; }}
      .hero {{ align-items: start; flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <div class="app">
    <div class="hero panel">
      <div>
        <div class="pill">实时可视化 · 中文界面 · 自动刷新</div>
        <h1>{title}</h1>
        <p>用于观察协调器、求解器、成本和追踪事件。页面会每 2 秒刷新一次，并保留当前选中的挑战与模型。</p>
      </div>
      <div class="row">
        <div class="pill">状态：<span id="state-label">连接中</span></div>
        <div class="pill">更新时间：<span id="updated-at">-</span></div>
      </div>
    </div>

    <div class="grid metrics" id="metrics"></div>

    <div class="panel" style="margin-top:18px;">
      <div class="panel-header">
        <div>
          <h2>操作入口</h2>
          <small>发送协调器消息，或对当前挑战执行暂停、恢复、重置和唤醒</small>
        </div>
        <div class="pill">本地控制</div>
      </div>
      <div class="panel-body">
        <div class="controls-grid">
          <input id="operator-message" class="input" type="text" placeholder="输入要发送给协调器的消息，例如：优先关注 web 类挑战并检查 trace。" />
          <div class="control-actions">
            <button id="send-message" class="button primary">发送消息</button>
            <button id="pause-challenge" class="button warn">暂停当前挑战</button>
            <button id="resume-challenge" class="button">恢复当前挑战</button>
            <button id="bump-challenge" class="button">唤醒当前模型</button>
            <button id="kill-challenge" class="button danger">终止当前挑战</button>
          </div>
          <div class="hint" id="control-hint" style="color: var(--muted); font-size: 12px;">当前操作会作用于右侧选中的挑战与模型。</div>
        </div>
      </div>
    </div>

    <div class="grid main" style="margin-top:18px;">
      <div class="panel">
        <div class="panel-header">
          <div>
            <h2>挑战总览</h2>
            <small>点击任意挑战，查看对应求解器详情与追踪日志</small>
          </div>
          <div class="pill">共 <span id="challenge-count">0</span> 个</div>
        </div>
        <div class="panel-body">
          <div class="split">
            <div id="challenge-list" class="list"></div>
            <div id="challenge-detail">
              <div class="empty">正在等待数据加载…</div>
            </div>
          </div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <div>
            <h2>追踪日志</h2>
            <small>展示最近的 tool call、返回结果、模型回复和状态事件</small>
          </div>
        </div>
        <div class="panel-body">
          <div class="trace-toolbar">
            <select id="trace-challenge" class="select"></select>
            <select id="trace-model" class="select"></select>
            <select id="trace-lines" class="select">
              <option value="20">最近 20 条</option>
              <option value="50" selected>最近 50 条</option>
              <option value="100">最近 100 条</option>
            </select>
            <button id="trace-refresh" class="select" style="cursor:pointer; min-width:auto; padding:10px 16px;">手动刷新</button>
          </div>
          <div id="trace-panel" class="trace" style="margin-top:14px;">
            <div class="empty">选择一个挑战和模型后，这里会显示最近的追踪日志。</div>
          </div>
        </div>
      </div>
    </div>

    <div class="panel" style="margin-top:18px;">
      <div class="panel-header">
        <div>
          <h2>模型成本</h2>
          <small>按模型聚合输入、缓存、输出 token 和累计花费</small>
        </div>
      </div>
      <div class="panel-body" id="cost-panel"></div>
    </div>

    <div class="footer">{title} · Dashboard 仅监听本机地址，适合在调试和竞赛期间本地查看。</div>
  </div>

  <script>
    const state = {{ data: null, selectedChallenge: null, selectedModel: null }};

    function fmtNum(value) {{
      if (value === null || value === undefined) return '-';
      if (typeof value !== 'number') return String(value);
      if (value >= 1_000_000) return (value / 1_000_000).toFixed(1) + 'M';
      if (value >= 1_000) return (value / 1_000).toFixed(1) + 'k';
      return String(value);
    }}

    function fmtCost(value) {{
      return '$' + (Number(value || 0)).toFixed(2);
    }}

    function statusBadgeClass(status) {{
      const s = String(status || '').toLowerCase();
      if (s.includes('solved') || s.includes('won') || s.includes('success')) return 'solved';
      if (s.includes('run')) return 'running';
      if (s.includes('pause')) return 'paused';
      if (s.includes('error') || s.includes('fail')) return 'error';
      return 'finished';
    }}

    function humanTime(ts) {{
      if (!ts) return '-';
      const d = new Date(ts * 1000);
      return d.toLocaleString('zh-CN', {{ hour12: false }});
    }}

    function selectedAgent() {{
      const challenges = state.data?.challenges || [];
      const challenge = challenges.find(c => c.name === state.selectedChallenge) || challenges[0];
      const agent = challenge?.agents?.find(a => a.spec === state.selectedModel) || challenge?.agents?.[0] || null;
      return {{ challenge, agent }};
    }}

    function renderMetrics(summary) {{
      const items = [
        ['活跃求解器', summary.active_swarms ?? 0, '当前仍在运行的 swarm 数量'],
        ['已解决', summary.solved ?? 0, '已确认完成的挑战数'],
        ['未解决', summary.unsolved ?? 0, '尚未解决的挑战数'],
        ['总花费', fmtCost(summary.total_cost_usd ?? 0), '所有 agent 的累计成本'],
        ['总 Token', fmtNum(summary.total_tokens ?? 0), '所有请求/响应 token 总量'],
        ['模型数', summary.model_count ?? 0, '当前配置的模型总数'],
      ];
      const container = document.getElementById('metrics');
      container.innerHTML = items.map(([label, value, hint], idx) => `
        <div class="panel metric">
          <div class="label">${label}</div>
          <div class="value ${idx === 3 ? 'status-warn' : idx === 1 ? 'status-ok' : idx === 0 ? 'status-info' : ''}">${value}</div>
          <div class="hint">${hint}</div>
        </div>
      `).join('');
    }}

    function renderChallenges(challenges) {{
      const list = document.getElementById('challenge-list');
      const count = document.getElementById('challenge-count');
      count.textContent = String(challenges.length);
      if (!challenges.length) {{
        list.innerHTML = '<div class="empty">暂无挑战数据。</div>';
        document.getElementById('challenge-detail').innerHTML = '<div class="empty">暂无挑战数据。</div>';
        return;
      }}
      if (!state.selectedChallenge || !challenges.some(c => c.name === state.selectedChallenge)) {{
        state.selectedChallenge = challenges[0].name;
      }}
      const active = challenges.find(c => c.name === state.selectedChallenge) || challenges[0];
      list.innerHTML = challenges.map(ch => `
        <div class="challenge ${ch.name === state.selectedChallenge ? 'active' : ''}" data-name="${escapeHtml(ch.name)}">
          <div class="challenge-top">
            <div>
              <h3>${escapeHtml(ch.name)}</h3>
              <div class="meta">
                ${escapeHtml(ch.category || '未知分类')} · ${fmtNum(ch.value || 0)} 分<br />
                解题数：${fmtNum(ch.solves || 0)}<br />
                ${escapeHtml(ch.description || '')}
              </div>
            </div>
            <span class="badge ${statusBadgeClass(ch.status)}">${escapeHtml(ch.status || 'finished')}</span>
          </div>
        </div>
      `).join('');
      list.querySelectorAll('.challenge').forEach(node => {{
        node.addEventListener('click', () => {{
          state.selectedChallenge = node.dataset.name;
          state.selectedModel = null;
          renderChallenges(challenges);
          renderDetail(challenges.find(c => c.name === state.selectedChallenge));
          syncSelectors();
          loadTrace();
        }});
      }});
      renderDetail(active);
      syncSelectors();
      updateControlHint();
    }}

    function renderDetail(challenge) {{
      const detail = document.getElementById('challenge-detail');
      if (!challenge) {{
        detail.innerHTML = '<div class="empty">请选择一个挑战查看详情。</div>';
        return;
      }}
      const agents = challenge.agents || [];
      if (!state.selectedModel || !agents.some(a => a.spec === state.selectedModel)) {{
        state.selectedModel = agents[0] ? agents[0].spec : null;
      }}
      const agentCards = agents.map(agent => `
        <div class="agent">
          <div class="agent-top">
            <div>
              <div><code>${escapeHtml(agent.spec || '')}</code></div>
              <div class="hint">${escapeHtml(agent.model_name || agent.model_id || '')} · ${escapeHtml(agent.provider || '')}</div>
            </div>
            <span class="badge ${statusBadgeClass(agent.status)}">${escapeHtml(agent.status || 'finished')}</span>
          </div>
          <div class="findings">${escapeHtml(agent.findings || '暂无最新发现')}</div>
          <div class="hint">步骤：${fmtNum(agent.step_count || 0)} · 花费：${fmtCost(agent.cost_usd || 0)} · 用时：${fmtNum(agent.duration_seconds || 0)}s</div>
          <div class="progress" style="margin-top:10px;"><span style="width:${Math.min(100, Math.max(5, agent.progress || 20))}%;"></span></div>
        </div>
      `).join('');
      detail.innerHTML = `
        <div class="challenge ${statusBadgeClass(challenge.status)}" style="cursor:default;">
          <div class="challenge-top">
            <div>
              <h3>${escapeHtml(challenge.name)}</h3>
              <div class="meta">
                ${escapeHtml(challenge.category || '未知分类')} · ${fmtNum(challenge.value || 0)} 分 · ${fmtNum(challenge.solves || 0)} 次解题<br />
                ${escapeHtml(challenge.description || '暂无描述')}
              </div>
            </div>
            <span class="badge ${statusBadgeClass(challenge.status)}">${escapeHtml(challenge.status || 'finished')}</span>
          </div>
          <div class="hint" style="margin-top:10px;">胜者：${escapeHtml(challenge.winner || '暂无')}</div>
          <div class="hint">暂停：${challenge.paused ? '是' : '否'} · 取消：${challenge.cancelled ? '是' : '否'}</div>
        </div>
        <div class="agents">${agentCards || '<div class="empty">暂无 agent。</div>'}</div>
      `;
    }}

    function renderCost(costByModel) {{
      const panel = document.getElementById('cost-panel');
      const entries = Object.entries(costByModel || {{}});
      if (!entries.length) {{
        panel.innerHTML = '<div class="empty">暂无成本数据。</div>';
        return;
      }}
      panel.innerHTML = entries.map(([model, data]) => `
        <div class="agent" style="margin-bottom:12px;">
          <div class="agent-top">
            <div>
              <div><code>${escapeHtml(model)}</code></div>
              <div class="hint">输入 ${fmtNum(data.input || 0)} · 缓存 ${fmtNum(data.cached || 0)} · 输出 ${fmtNum(data.output || 0)}</div>
            </div>
            <span class="badge running">${fmtCost(data.cost || 0)}</span>
          </div>
          <div class="progress" style="margin-top:10px;"><span style="width:${Math.min(100, 10 + (Number(data.cost || 0) * 10))}%;"></span></div>
        </div>
      `).join('');
    }}

    function syncSelectors() {{
      const traceChallenge = document.getElementById('trace-challenge');
      const traceModel = document.getElementById('trace-model');
      const challenges = state.data?.challenges || [];
      const active = challenges.find(c => c.name === state.selectedChallenge) || challenges[0];
      traceChallenge.innerHTML = challenges.map(ch => `<option value="${escapeHtml(ch.name)}">${escapeHtml(ch.name)}</option>`).join('');
      if (state.selectedChallenge) traceChallenge.value = state.selectedChallenge;
      const agents = active?.agents || [];
      traceModel.innerHTML = agents.map(a => `<option value="${escapeHtml(a.spec)}">${escapeHtml(a.spec)}</option>`).join('');
      if (!state.selectedModel || !agents.some(a => a.spec === state.selectedModel)) {{
        state.selectedModel = agents[0] ? agents[0].spec : null;
      }}
      if (state.selectedModel) traceModel.value = state.selectedModel;
    }}

    async function loadTrace() {{
      const challenge = state.selectedChallenge;
      const model = state.selectedModel;
      const lines = Number(document.getElementById('trace-lines').value || '50');
      if (!challenge || !model) return;
      const panel = document.getElementById('trace-panel');
      panel.innerHTML = '<div class="empty">正在加载追踪日志…</div>';
      try {{
        const resp = await fetch(`/api/trace?challenge=${encodeURIComponent(challenge)}&model=${encodeURIComponent(model)}&lines=${lines}`);
        const data = await resp.json();
        const items = data.items || [];
        if (!items.length) {{
          panel.innerHTML = '<div class="empty">暂无追踪日志。</div>';
          return;
        }}
        panel.innerHTML = items.map(item => `
          <div class="trace-line">
            <div class="trace-time">${escapeHtml(item.time || '-')}</div>
            <div class="trace-type">${escapeHtml(item.type || 'event')}</div>
            <div class="trace-text">${escapeHtml(item.text || '')}</div>
          </div>
        `).join('');
      }} catch (err) {{
        panel.innerHTML = `<div class="empty">加载失败：${escapeHtml(String(err))}</div>`;
      }}
    }}

    function escapeHtml(value) {{
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }}

    function updateControlHint() {{
      const {{ challenge, agent }} = selectedAgent();
      const text = challenge
        ? `当前操作作用于：${challenge.name}${agent ? ' / ' + agent.spec : ''}`
        : '暂无可操作的挑战。';
      document.getElementById('control-hint').textContent = text;
    }}

    async function sendControl(action, extra = {{}}) {{
      const payload = {{ action, challenge: state.selectedChallenge, model: state.selectedModel, ...extra }};
      try {{
        const resp = await fetch('/api/control', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload),
        }});
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || '操作失败');
        return data;
      }} catch (err) {{
        alert(String(err));
        throw err;
      }}
    }}

    async function refresh() {{
      try {{
        const resp = await fetch('/api/state');
        const data = await resp.json();
        state.data = data;
        document.getElementById('state-label').textContent = data.summary?.status_text || '运行中';
        document.getElementById('updated-at').textContent = humanTime(data.updated_at);
        renderMetrics(data.summary || {{}});
        renderChallenges(data.challenges || []);
        renderCost(data.cost_by_model || {{}});
        const traceChallenge = document.getElementById('trace-challenge');
        const traceModel = document.getElementById('trace-model');
        traceChallenge.onchange = () => {{
          state.selectedChallenge = traceChallenge.value;
          const active = (state.data?.challenges || []).find(c => c.name === state.selectedChallenge);
          state.selectedModel = active?.agents?.[0]?.spec || null;
          renderChallenges(state.data?.challenges || []);
          syncSelectors();
          loadTrace();
        }};
        traceModel.onchange = () => {{
          state.selectedModel = traceModel.value;
          loadTrace();
          updateControlHint();
        }};
        document.getElementById('trace-lines').onchange = loadTrace;
        document.getElementById('trace-refresh').onclick = loadTrace;
        document.getElementById('send-message').onclick = async () => {{
          const msg = document.getElementById('operator-message').value.trim();
          if (!msg) return;
          await sendControl('message', {{ message: msg }});
          document.getElementById('operator-message').value = '';
          await refresh();
        }};
        document.getElementById('pause-challenge').onclick = async () => {{
          await sendControl('pause');
          await refresh();
        }};
        document.getElementById('resume-challenge').onclick = async () => {{
          await sendControl('resume');
          await refresh();
        }};
        document.getElementById('bump-challenge').onclick = async () => {{
          const msg = document.getElementById('operator-message').value.trim() || '请继续深入分析，尝试不同的思路。';
          await sendControl('bump', {{ message: msg }});
          await refresh();
        }};
        document.getElementById('kill-challenge').onclick = async () => {{
          await sendControl('kill');
          await refresh();
        }};
        if (!state.selectedChallenge && (data.challenges || []).length) {{
          state.selectedChallenge = data.challenges[0].name;
        }}
        if (!state.selectedModel && (data.challenges || [])[0]?.agents?.length) {{
          state.selectedModel = data.challenges[0].agents[0].spec;
        }}
        syncSelectors();
        updateControlHint();
        await loadTrace();
      }} catch (err) {{
        document.getElementById('state-label').textContent = '离线';
        document.getElementById('challenge-list').innerHTML = '<div class="empty">无法连接到后台状态接口。</div>';
        document.getElementById('challenge-detail').innerHTML = `<div class="empty">${escapeHtml(String(err))}</div>`;
      }}
    }}

    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"""


@dataclass
class DashboardServer:
    """Background HTTP server for the local dashboard."""

    server: ThreadingHTTPServer
    thread: threading.Thread
    host: str
    port: int

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def close(self) -> None:
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.server.server_close()
        except Exception:
            pass
        if self.thread.is_alive():
            self.thread.join(timeout=2)


class _DashboardHandler(BaseHTTPRequestHandler):
    snapshot_fn: SnapshotFn
    trace_fn: TraceFn
    control_fn: ControlFn | None
    title: str

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_html()
            return
        if parsed.path == "/api/state":
            self._send_json(self.snapshot_fn())
            return
        if parsed.path == "/api/trace":
            params = parse_qs(parsed.query)
            challenge = params.get("challenge", [""])[0]
            model = params.get("model", [""])[0]
            lines = _safe_int(params.get("lines", ["50"])[0], 50)
            payload = {"items": self.trace_fn(challenge, model, lines)}
            self._send_json(payload)
            return
        self.send_error(404, "Not Found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/control":
            self.send_error(404, "Not Found")
            return
        if not self.control_fn:
            self._send_json({"error": "control api not configured"}, status=400)
            return

        content_length = _safe_int(self.headers.get("Content-Length", "0"), 0)
        raw = self.rfile.read(max(0, content_length)) if content_length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            self._send_json({"error": "invalid json"}, status=400)
            return

        try:
            result = self.control_fn(payload)
            self._send_json(result)
        except Exception as exc:
            logger.warning("control api failed: %s", exc)
            self._send_json({"error": str(exc)}, status=500)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        logger.debug(format, *args)

    def _send_html(self) -> None:
        content = HTML_TEMPLATE.replace("{title}", html.escape(self.title))
        encoded = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def start_dashboard_server(
    *,
    host: str,
    port: int,
    title: str,
    snapshot_fn: SnapshotFn,
    trace_fn: TraceFn,
    control_fn: ControlFn | None = None,
) -> DashboardServer | None:
    """Start a local dashboard server in a background thread."""
    if port <= 0:
        return None

    handler_cls = type(
        "AgentDashboardHandler",
        (_DashboardHandler,),
        {
            "snapshot_fn": staticmethod(snapshot_fn),
            "trace_fn": staticmethod(trace_fn),
            "control_fn": staticmethod(control_fn) if control_fn else None,
            "title": title,
        },
    )

    try:
        server = ThreadingHTTPServer((host, port), handler_cls)
    except OSError as exc:
        logger.warning("Could not start dashboard server on %s:%s: %s", host, port, exc)
        return None

    thread = threading.Thread(target=server.serve_forever, name="agent-dashboard", daemon=True)
    thread.start()
    actual_host, actual_port = server.server_address[:2]
    logger.info("Dashboard listening on http://%s:%s", actual_host, actual_port)
    return DashboardServer(server=server, thread=thread, host=str(actual_host), port=int(actual_port))


def _safe_int(value: str, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def read_trace_events(path: str, lines: int = 50) -> list[dict[str, Any]]:
    """Read and normalize the newest trace events from a JSONL file."""
    file_path = Path(path)
    if not file_path.exists():
        return []

    try:
        with file_path.open(encoding="utf-8", errors="replace") as stream:
            raw_lines = list(deque(stream, maxlen=max(1, lines)))
    except Exception:
        return []

    items: list[dict[str, Any]] = []
    for raw in raw_lines:
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            items.append({"time": "-", "type": "raw", "text": raw[:500]})
            continue

        ts = data.get("ts")
        time_text = "-"
        if isinstance(ts, (int, float)):
            import datetime as _dt

            time_text = _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")

        kind = str(data.get("type", "event"))
        if kind == "tool_call":
            text = f"step {data.get('step', '?')} 调用 {data.get('tool', '?')}\n参数：{data.get('args', '')}"
        elif kind == "tool_result":
            text = f"step {data.get('step', '?')} 返回 {data.get('tool', '?')}\n结果：{data.get('result', '')}"
        elif kind == "model_response":
            text = f"step {data.get('step', '?')} 模型回复\n{text_or_empty(data.get('text', ''))}"
        elif kind == "usage":
            text = (
                f"输入 {data.get('input_tokens', 0)} / 输出 {data.get('output_tokens', 0)} / "
                f"缓存 {data.get('cache_read_tokens', 0)} / 花费 ${float(data.get('cost_usd', 0)):.4f}"
            )
        else:
            payload = {k: v for k, v in data.items() if k != "ts"}
            text = json.dumps(payload, ensure_ascii=False, indent=2)

        items.append({"time": time_text, "type": kind, "text": text})
    return items


def text_or_empty(value: Any) -> str:
    text = str(value or "")
    return text[:2000]
