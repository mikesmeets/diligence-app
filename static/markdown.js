/*
 * Shared Markdown rendering and a small editor for the textareas that feed it.
 *
 * A deliberately narrow subset — headings, lists, quotes, rules, tables, and
 * inline bold/italic/code. Everything is HTML-escaped before any markup is
 * added, so model output and typed notes alike are inert.
 *
 * renderMarkdown(src, {breaks}) — breaks:true treats a single newline as a
 * line break, which is what people expect in a notes field. Long-form prose
 * pages leave it off and get standard paragraph behaviour.
 */
(function (global) {
  'use strict';

  function esc(s) {
    return String(s ?? '')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // Figures get right-aligned even when alignment markers are omitted — a
  // column of left-ragged numbers is unreadable.
  const NUMERIC = /^[-+(]?\s*[$€£]?\s*\d[\d,]*(\.\d+)?\s*(%|x|bps|bn|mm|tn|k)?\s*\)?$/i;

  function inline(t) {
    return esc(t)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  }

  const cells = (row) =>
    row.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
  const isRow = (l) => /^\s*\|.*\|\s*$/.test(l || '');
  const isSep = (l) => /^\s*\|[\s:|-]*-[\s:|-]*\|\s*$/.test(l || '');

  function renderMarkdown(src, options) {
    const breaks = !!(options && options.breaks);
    const lines = String(src ?? '').split('\n');
    const out = [];
    let list = null, para = [];

    const flushPara = () => {
      if (!para.length) return;
      out.push(`<p>${para.map(inline).join(breaks ? '<br>' : ' ')}</p>`);
      para = [];
    };
    const flushList = () => {
      if (list) { out.push(`</${list}>`); list = null; }
    };

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trimEnd();

      // GFM table: a header row followed by a |---|---| separator.
      if (isRow(line) && isSep(lines[i + 1])) {
        flushPara(); flushList();
        const head = cells(line);
        const marks = cells(lines[i + 1]).map(s =>
          /^:-+:$/.test(s) ? 'center' : /^-+:$/.test(s) ? 'right'
          : /^:-+$/.test(s) ? 'left' : null);
        i += 1;

        const body = [];
        while (isRow(lines[i + 1])) body.push(cells(lines[++i]));

        // One alignment per column, so a header sits over its own figures.
        const align = head.map((_, n) => {
          if (marks[n]) return marks[n];
          const vals = body.map(r => r[n]).filter(v => v !== undefined && v !== '');
          return vals.length && vals.every(v => NUMERIC.test(v)) ? 'right' : null;
        });
        const style = (n) => align[n] ? ` style="text-align:${align[n]}"` : '';

        out.push('<div class="md-table"><table><thead><tr>' +
          head.map((h, n) => `<th${style(n)}>${inline(h)}</th>`).join('') +
          '</tr></thead><tbody>' +
          body.map(r => '<tr>' + r.map((c, n) => `<td${style(n)}>${inline(c)}</td>`).join('') + '</tr>').join('') +
          '</tbody></table></div>');
        continue;
      }

      if (!line.trim()) { flushPara(); flushList(); continue; }
      if (/^\s*(---+|\*\*\*+)\s*$/.test(line)) { flushPara(); flushList(); out.push('<hr>'); continue; }

      const h = line.match(/^(#{1,3})\s+(.*)$/);
      if (h) { flushPara(); flushList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }

      const q = line.match(/^>\s?(.*)$/);
      if (q) { flushPara(); flushList(); out.push(`<blockquote>${inline(q[1])}</blockquote>`); continue; }

      const ul = line.match(/^\s*[-*+]\s+(.*)$/);
      const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        flushPara();
        const want = ul ? 'ul' : 'ol';
        if (list !== want) { flushList(); out.push(`<${want}>`); list = want; }
        out.push(`<li>${inline((ul || ol)[1])}</li>`);
        continue;
      }

      flushList();
      para.push(line.trim());
    }
    flushPara(); flushList();
    return out.join('\n');
  }

  // ── Editing helpers ──────────────────────────────────────────────────────

  function surround(ta, before, after) {
    const { selectionStart: s, selectionEnd: e, value } = ta;
    const picked = value.slice(s, e) || '';
    ta.value = value.slice(0, s) + before + picked + after + value.slice(e);
    // Leave the cursor inside the markers when nothing was selected.
    const caret = s + before.length + picked.length;
    ta.focus();
    ta.setSelectionRange(picked ? s + before.length : caret, caret);
  }

  function prefixLines(ta, makePrefix) {
    const { selectionStart: s, selectionEnd: e, value } = ta;
    const start = value.lastIndexOf('\n', s - 1) + 1;
    const end   = value.indexOf('\n', e) === -1 ? value.length : value.indexOf('\n', e);
    const block = value.slice(start, end);

    const lines = block.split('\n');
    // Toggle off when every line already carries the marker.
    const already = lines.every(l => !l.trim() || /^\s*([-*+]|\d+[.)]|>|#{1,3})\s/.test(l));
    const changed = lines.map((l, i) => {
      if (!l.trim()) return l;
      const bare = l.replace(/^\s*([-*+]|\d+[.)]|>|#{1,3})\s+/, '');
      return already ? bare : makePrefix(i) + bare;
    }).join('\n');

    ta.value = value.slice(0, start) + changed + value.slice(end);
    ta.focus();
    ta.setSelectionRange(start, start + changed.length);
  }

  const ACTIONS = {
    bold:    (ta) => surround(ta, '**', '**'),
    italic:  (ta) => surround(ta, '*', '*'),
    code:    (ta) => surround(ta, '`', '`'),
    bullet:  (ta) => prefixLines(ta, () => '- '),
    number:  (ta) => prefixLines(ta, (i) => `${i + 1}. `),
    heading: (ta) => prefixLines(ta, () => '## '),
    quote:   (ta) => prefixLines(ta, () => '> '),
  };

  function applyFormat(textarea, action) {
    const fn = ACTIONS[action];
    if (fn) fn(textarea);
  }

  /* Keyboard shortcuts, plus the thing that makes lists usable: pressing Enter
     inside one continues it, and pressing Enter on an empty item ends it. */
  function attachEditor(ta) {
    if (!ta || ta.dataset.mdEditor) return;
    ta.dataset.mdEditor = '1';

    ta.addEventListener('keydown', (e) => {
      const meta = e.metaKey || e.ctrlKey;
      if (meta && e.key.toLowerCase() === 'b') { e.preventDefault(); applyFormat(ta, 'bold');   return; }
      if (meta && e.key.toLowerCase() === 'i') { e.preventDefault(); applyFormat(ta, 'italic'); return; }
      if (e.key !== 'Enter' || e.shiftKey) return;

      const pos  = ta.selectionStart;
      const from = ta.value.lastIndexOf('\n', pos - 1) + 1;
      const line = ta.value.slice(from, pos);

      const bullet = line.match(/^(\s*)([-*+])\s+(.*)$/);
      const number = line.match(/^(\s*)(\d+)[.)]\s+(.*)$/);
      if (!bullet && !number) return;

      e.preventDefault();
      const [, indent, marker, rest] = bullet || number;
      if (!rest.trim()) {
        // Empty item — drop the marker and leave the list.
        ta.value = ta.value.slice(0, from) + ta.value.slice(pos);
        ta.setSelectionRange(from, from);
        return;
      }
      const next = bullet ? `${indent}${marker} ` : `${indent}${parseInt(marker, 10) + 1}. `;
      ta.value = ta.value.slice(0, pos) + '\n' + next + ta.value.slice(pos);
      ta.setSelectionRange(pos + 1 + next.length, pos + 1 + next.length);
    });
  }

  /* Toolbar markup. `target` is the id of the textarea it drives. */
  function toolbarHtml(target) {
    const btn = (action, label, title) =>
      `<button type="button" title="${title}" onmousedown="event.preventDefault()"
         onclick="applyFormat(document.getElementById('${target}'), '${action}')"
         class="px-2 py-1 text-xs text-gray-500 hover:text-gray-900 hover:bg-gray-100 rounded transition-colors">${label}</button>`;
    return `<div class="flex items-center gap-0.5 flex-wrap border border-gray-200 border-b-0 rounded-t-lg px-1 py-1 bg-gray-50">
      ${btn('bold', '<strong>B</strong>', 'Bold (Ctrl+B)')}
      ${btn('italic', '<em>I</em>', 'Italic (Ctrl+I)')}
      ${btn('code', '&lt;/&gt;', 'Code')}
      <span class="w-px h-4 bg-gray-200 mx-1"></span>
      ${btn('bullet', '• List', 'Bullet list')}
      ${btn('number', '1. List', 'Numbered list')}
      ${btn('heading', 'H', 'Heading')}
      ${btn('quote', '&ldquo;', 'Quote')}
    </div>`;
  }

  global.renderMarkdown = renderMarkdown;
  global.applyFormat = applyFormat;
  global.attachMarkdownEditor = attachEditor;
  global.markdownToolbar = toolbarHtml;
})(window);
