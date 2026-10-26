"""Surface layer: how we perceive and act on an application.

Everything above this module (agent, recorder, replay) speaks in terms of
`Target`/`Locator`/`Condition` from the schema. Only this module knows about
Playwright, the DOM, and frames. A desktop surface (UIA / AX accessibility tree)
would implement the same methods and map the same locator strategies:
role -> control type + name, label_cell -> labelled-by / spatial neighbour,
row_cell -> grid row/column, css -> automation-id path.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import BrowserContext, Frame, Locator as PWLocator, Page, sync_playwright

from .schema import Condition, Locator, Target

# --------------------------------------------------------------------------- JS

_OBSERVE_JS = r"""
(prefix) => {
  document.querySelectorAll('[data-cua-ref]').forEach(e => e.removeAttribute('data-cua-ref'));
  let n = 0;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const INTERACTIVE = 'a[href],button,input:not([type=hidden]),select,textarea,[onclick],[role=button],[role=link]';
  const tag = el => { const r = prefix + 'e' + (++n); el.setAttribute('data-cua-ref', r); return r; };
  const describe = el => {
    const t = el.tagName.toLowerCase(), ty = (el.type || '').toLowerCase();
    if (t === 'a') return `link "${norm(el.innerText)}"`;
    if (t === 'select') return `select [${[...el.options].map(o => o.text).join(' | ')}] selected="${el.selectedOptions[0]?.text || ''}"`;
    if (t === 'input' && ['submit', 'button', 'reset', 'image'].includes(ty)) return `button "${el.value || el.alt || ''}"`;
    if (t === 'input' && (ty === 'checkbox' || ty === 'radio')) return `${ty}${el.checked ? ' (checked)' : ''}`;
    if (t === 'input' || t === 'textarea') return ty === 'password' ? 'password-box' : `textbox value="${el.value}"`;
    if (t === 'button') return `button "${norm(el.innerText)}"`;
    return `clickable "${norm(el.innerText)}"`;
  };
  const lines = [];
  const walk = (node, out) => {
    if (node.nodeType === 3) { const s = norm(node.textContent); if (s) out.push(s); return; }
    if (node.nodeType !== 1) return;
    const el = node;
    if (['SCRIPT', 'STYLE', 'NOSCRIPT', 'HEAD'].includes(el.tagName)) return;
    if (!['TR', 'TBODY', 'THEAD', 'FORM'].includes(el.tagName) && !visible(el)) return;
    if (el.matches(INTERACTIVE)) { out.push(`[${tag(el)}] ${describe(el)}`); return; }
    if (el.tagName === 'TR' && !el.querySelector('table')) {
      const cells = [...el.cells].map(c => {
        if (c.querySelector(INTERACTIVE)) { const sub = []; c.childNodes.forEach(ch => walk(ch, sub)); return sub.join(' '); }
        const s = norm(c.innerText); return s ? `[${tag(c)}] ${s}` : '';
      }).filter(Boolean);
      if (cells.length) lines.push('row: ' + cells.join(' | '));
      return;
    }
    const block = [];
    el.childNodes.forEach(ch => walk(ch, block));
    if (block.length) (out === lines ? lines.push(block.join(' ')) : out.push(...block));
  };
  if (document.body) document.body.childNodes.forEach(ch => walk(ch, lines));
  return lines.filter(Boolean);
}
"""

# Facts about one element, used by the recorder to build locator candidates.
_DESCRIBE_JS = r"""
(el) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const t = el.tagName.toLowerCase(), ty = (el.type || '').toLowerCase();
  let role = null, name = null;
  if (t === 'a' && el.hasAttribute('href')) { role = 'link'; name = norm(el.innerText); }
  else if (t === 'button' || (t === 'input' && ['submit', 'button', 'reset'].includes(ty))) { role = 'button'; name = t === 'button' ? norm(el.innerText) : el.value; }
  else if (t === 'select') role = 'combobox';
  else if (t === 'textarea' || (t === 'input' && !['checkbox', 'radio', 'hidden'].includes(ty))) role = 'textbox';
  name = el.getAttribute('aria-label') || name;
  // label cell: the nearest preceding sibling <td> with text, legacy form idiom
  let labelCell = null;
  const td = el.closest('td');
  if (td && el !== td) { let p = td.previousElementSibling; while (p && !norm(p.innerText)) p = p.previousElementSibling; if (p) labelCell = norm(p.innerText); }
  // row context: the row's cells and the table's header row
  let row = null;
  const cell = el.closest('td,th');
  if (cell) {
    const tr = cell.parentElement, table = tr.closest('table');
    const header = table ? [...table.rows].find(r => r !== tr && [...r.cells].some(c => norm(c.innerText))) : null;
    row = { index: cell.cellIndex,
            cells: [...tr.cells].map(c => norm(c.innerText)),
            headers: header && header !== tr && header.cells.length === tr.cells.length ? [...header.cells].map(c => norm(c.innerText)) : null };
  }
  const path = []; let cur = el;
  while (cur && cur.nodeType === 1 && cur.tagName !== 'HTML') {
    let i = 1, s = cur; while ((s = s.previousElementSibling)) if (s.tagName === cur.tagName) i++;
    path.unshift(cur.tagName.toLowerCase() + ':nth-of-type(' + i + ')'); cur = cur.parentElement;
  }
  return { tag: t, type: ty, role, name: name ? norm(name) : null, text: norm(el.innerText || el.value || ''),
           nameAttr: el.getAttribute('name'), href: el.getAttribute('href'), baseUrl: document.baseURI,
           labelCell, row, cssPath: path.join(' > ') };
}
"""

# Legacy-idiom resolvers. Executed in-page; mark hits so Playwright can address them.
_RESOLVE_JS = r"""
([strategy, args, token]) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('[data-cua-hit]').forEach(e => e.removeAttribute('data-cua-hit'));
  let hits = [];
  if (strategy === 'label_cell') {
    for (const td of document.querySelectorAll('td,th')) {
      if (norm(td.innerText) !== args.label) continue;
      let n = td.nextElementSibling;
      while (n && !n.querySelector('input:not([type=hidden]),select,textarea')) n = n.nextElementSibling;
      if (n) hits.push(n.querySelector('input:not([type=hidden]),select,textarea'));
    }
  } else if (strategy === 'row_cell') {
    for (const table of document.querySelectorAll('table')) {
      const rows = [...table.rows];
      const header = rows.find(r => [...r.cells].some(c => norm(c.innerText) === args.key_column));
      if (!header) continue;
      const kc = [...header.cells].findIndex(c => norm(c.innerText) === args.key_column);
      const vc = args.column != null ? [...header.cells].findIndex(c => norm(c.innerText) === args.column) : args.index;
      if (kc < 0 || vc == null || vc < 0) continue;
      for (const r of rows) {
        if (r === header || !r.cells[kc] || norm(r.cells[kc].innerText) !== args.key) continue;
        const c = r.cells[vc]; if (!c) continue;
        const inner = args.pick ? c.querySelector(args.pick) : null;
        hits.push(inner || c);
      }
    }
  }
  hits.forEach(h => h.setAttribute('data-cua-hit', token));
  return hits.length;
}
"""

# Captures what a human does while they hold control (installed in every frame).
_HUMAN_CAPTURE_JS = r"""
(() => {
  if (window.__cuaHooked) return; window.__cuaHooked = true;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const d = el => ({ tag: el.tagName.toLowerCase(), name: el.getAttribute && el.getAttribute('name'),
    text: el.type === 'password' ? '***' : norm(el.innerText || (['submit', 'button'].includes(el.type) ? el.value : '')) });
  const send = ev => { try { window.__cuaHumanEvent && window.__cuaHumanEvent(ev); } catch (e) {} };
  document.addEventListener('click', e => send({ type: 'click', el: d(e.target), url: location.pathname }), true);
  document.addEventListener('change', e => { const t = e.target;
    send({ type: 'input', el: d(t), value: t.type === 'password' ? '***' : t.value, url: location.pathname }); }, true);
  document.addEventListener('submit', e => send({ type: 'submit', url: location.pathname }), true);
})();
"""


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def substitute(s: str, values: dict[str, str]) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), s)


@dataclass
class Resolution:
    locator: PWLocator | None
    strategy: str | None  # e.g. "role", "css"; None when not found
    index: int = -1
    ambiguous: list[str] = field(default_factory=list)


class WebSurface:
    """Playwright-backed surface. One live browser session per instance."""

    def __init__(self, headless: bool = True, cdp_port: int | None = 9333, user_data_dir: str | None = None):
        self._pw = sync_playwright().start()
        args = [f"--remote-debugging-port={cdp_port}"] if cdp_port else []
        # Persistent context = the browser's default context, so an operator attaching over
        # CDP sees the very same pages/cookies (the live session), not a fresh one.
        self.context: BrowserContext = self._pw.chromium.launch_persistent_context(
            user_data_dir or "", headless=headless, args=args, viewport={"width": 1100, "height": 760}
        )
        self.cdp_endpoint = f"http://127.0.0.1:{cdp_port}" if cdp_port else None
        self.page: Page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(10_000)
        self._human_handler: Callable[[dict], None] | None = None
        self.context.expose_binding("__cuaHumanEvent", lambda _src, ev: self._human_handler and self._human_handler(ev))
        self.context.add_init_script(_HUMAN_CAPTURE_JS)

    def close(self) -> None:
        try:
            self.context.close()
        finally:
            self._pw.stop()

    # ------------------------------------------------------------ frames

    def frames(self) -> list[Frame]:
        return [f for f in self.page.frames if not f.is_detached()]

    def frame(self, name: str | None) -> Frame | None:
        if name is None:
            return self.page.main_frame
        return self.page.frame(name=name)

    def frame_name(self, f: Frame) -> str | None:
        return None if f == self.page.main_frame else (f.name or None)

    def on_human_event(self, handler: Callable[[dict], None] | None) -> None:
        self._human_handler = handler

    # ------------------------------------------------------------ perception

    def observe(self) -> tuple[str, dict[str, tuple[str | None, str]]]:
        """Text rendering of every frame with ref ids for actionable/readable elements.
        Returns (text, refs) where refs maps ref -> (frame name, ref)."""
        out: list[str] = [f"URL: {self.page.url}", f"Title: {self.page.title()}"]
        refs: dict[str, tuple[str | None, str]] = {}
        for i, f in enumerate(self.frames()):
            name = self.frame_name(f)
            prefix = "" if name is None else f"f{i}"
            try:
                lines = f.evaluate(_OBSERVE_JS, prefix)
            except Exception as e:  # frame navigated mid-read
                lines = [f"(frame unreadable: {type(e).__name__})"]
            if not lines:
                continue
            label = "top document" if name is None else f'frame "{name}" ({f.url.split("?")[0]})'
            out.append(f"--- {label} ---")
            out.extend(lines)
            for line in lines:
                for r in re.findall(r"\[((?:f\d+)?e\d+)\]", line):
                    refs[r] = (name, r)
        return "\n".join(out), refs

    def visible_text(self) -> dict[str | None, str]:
        texts: dict[str | None, str] = {}
        for f in self.frames():
            try:
                texts[self.frame_name(f)] = f.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:
                pass
        return texts

    def by_ref(self, frame: str | None, ref: str) -> PWLocator:
        f = self.frame(frame)
        if f is None:
            raise LookupError(f"frame {frame!r} not present")
        return f.locator(f'[data-cua-ref="{ref}"]')

    def describe(self, loc: PWLocator) -> dict[str, Any]:
        return loc.evaluate(_DESCRIBE_JS)

    # ------------------------------------------------------------ targeting

    def _pw_locator(self, f: Frame, loc: Locator, values: dict[str, str]) -> PWLocator:
        a = {k: substitute(v, values) if isinstance(v, str) else v for k, v in loc.args.items()}
        if loc.strategy == "role":
            return f.get_by_role(a["role"], name=a["name"], exact=True)
        if loc.strategy == "text":
            return f.get_by_text(a["text"], exact=True)
        if loc.strategy == "css":
            return f.locator(a["css"])
        token = f"h{time.monotonic_ns()}"
        f.evaluate(_RESOLVE_JS, [loc.strategy, a, token])
        return f.locator(f'[data-cua-hit="{token}"]')

    def try_resolve(self, target: Target, values: dict[str, str]) -> Resolution:
        """One pass over the candidates, most robust first. A candidate counts only
        if it matches exactly one element; ambiguity is never 'first match wins'."""
        f = self.frame(target.frame)
        if f is None:
            return Resolution(None, None)
        ambiguous = []
        for i, cand in enumerate(target.locators):
            try:
                pl = self._pw_locator(f, cand, values)
                n = pl.count()
            except Exception:
                continue
            if n == 1:
                return Resolution(pl, cand.strategy, i, ambiguous)
            if n > 1:
                ambiguous.append(cand.strategy)
        return Resolution(None, None, -1, ambiguous)

    def same_element(self, a: PWLocator, b: PWLocator) -> bool:
        try:
            mark = f"s{time.monotonic_ns()}"
            a.evaluate("(el, m) => el.setAttribute('data-cua-same', m)", mark)
            return b.evaluate("(el, m) => el.getAttribute('data-cua-same') === m", mark)
        except Exception:
            return False

    def candidate_locator(self, frame: str | None, cand: Locator, values: dict[str, str]) -> PWLocator | None:
        f = self.frame(frame)
        return self._pw_locator(f, cand, values) if f else None

    # ------------------------------------------------------------ actions

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")

    def click(self, loc: PWLocator) -> None:
        loc.click(timeout=5_000)
        self.settle()

    def fill(self, loc: PWLocator, value: str) -> None:
        loc.fill(value, timeout=5_000)

    def select(self, loc: PWLocator, value: str) -> None:
        loc.select_option(label=value, timeout=5_000)

    def read(self, loc: PWLocator) -> str:
        return _ws(loc.inner_text(timeout=5_000))

    def settle(self, ms: int = 400) -> None:
        """Let navigation start, then wait for every frame's DOM."""
        self.page.wait_for_timeout(ms)
        for f in self.frames():
            try:
                f.wait_for_load_state("domcontentloaded", timeout=5_000)
            except Exception:
                pass

    def idle(self, ms: int) -> None:
        # wait_for_timeout keeps Playwright's event loop pumping (bindings, routes)
        self.page.wait_for_timeout(ms)

    # ------------------------------------------------------------ conditions

    def check(self, c: Condition, values: dict[str, str], texts: dict[str | None, str] | None = None) -> bool:
        if c.kind in ("text_visible", "text_absent"):
            texts = texts if texts is not None else self.visible_text()
            needle = substitute(c.value or "", values)
            pool = [texts.get(c.frame, "")] if c.frame is not None else list(texts.values())
            hit = any(needle in t for t in pool)
            return hit if c.kind == "text_visible" else not hit
        if c.kind == "url_matches":
            f = self.frame(c.frame)
            return bool(f and re.search(substitute(c.value or "", values), f.url))
        if c.kind == "target_visible" and c.target:
            return self.try_resolve(c.target, values).locator is not None
        return False

    # ------------------------------------------------------------ evidence

    def screenshot(self, path: Path) -> str:
        """Full-page screenshot with PII masked: form inputs and any table cell
        containing digits (member #s, balances, dates) are blacked out."""
        masks: list[PWLocator] = []
        for f in self.frames():
            masks.append(f.locator("input:not([type=submit]):not([type=button]):not([type=reset])"))
            masks.append(f.locator("td").filter(has_text=re.compile(r"\d")).filter(has_not=f.locator("table")))
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), mask=masks, mask_color="#000")
        return str(path)
