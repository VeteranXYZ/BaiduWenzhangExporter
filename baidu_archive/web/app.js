/* 全部查询与导出在设备上完成，不访问网络。 */
(() => {
  'use strict';
  const data = window.ARCHIVE_DATA;
  const $ = id => document.getElementById(id);
  const selected = new Set();
  let filter = 'all';
  let visible = [];
  const labels = {all: '收藏目录', ready: '可离线阅读', pending: '待归档文章', other: '其他收藏'};
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const remaining = Number.isInteger(data.total) ? Math.max(0, data.total - data.indexed) : null;
  $('progress-text').textContent = remaining === null
    ? `已保存 ${data.ready} 篇正文；账号总数尚未确认。`
    : remaining > 0
      ? `当前为部分归档：${data.indexed} / ${data.total} 条已索引，还有 ${remaining} 条尚未建立索引。`
      : `全部 ${data.indexed} 条已建立索引，${data.ready} 篇正文可离线阅读。`;
  [...new Set(data.entries.map(e => e.year))].sort().reverse().forEach(year => {
    const option = element('option', '', year); option.value = year; $('year').append(option);
  });
  function updateSelection() {
    $('selection-count').textContent = `已选 ${selected.size} 篇`;
    $('export').disabled = selected.size === 0;
    const ready = visible.filter(e => e.available);
    const count = ready.filter(e => selected.has(e.key)).length;
    $('select-all').disabled = ready.length === 0;
    $('select-all').checked = ready.length > 0 && count === ready.length;
    $('select-all').indeterminate = count > 0 && count < ready.length;
  }
  function render() {
    const query = $('q').value.trim().toLocaleLowerCase();
    const year = $('year').value;
    visible = data.entries.filter(e =>
      (filter === 'all' || (filter === 'ready' && e.available) ||
       (filter === 'pending' && e.type === 'page' && !e.available) ||
       (filter === 'other' && e.type !== 'page')) &&
      (!year || e.year === year) &&
      (!query || [e.title, e.text, e.excerpt, e.source].join(' ').toLocaleLowerCase().includes(query))
    );
    $('list').replaceChildren();
    visible.forEach(entry => {
      const row = element('div', 'record' + (selected.has(entry.key) ? ' selected' : ''));
      const check = element('input'); check.type = 'checkbox'; check.disabled = !entry.available;
      check.checked = selected.has(entry.key); check.setAttribute('aria-label', `选择 ${entry.title}`);
      check.addEventListener('change', () => {
        if (check.checked) selected.add(entry.key); else selected.delete(entry.key);
        row.classList.toggle('selected', check.checked); updateSelection();
      });
      const date = element('div', 'record-date');
      date.append(element('strong', '', entry.date ? entry.date.slice(5).replace('-', '.') : '—'), element('span', '', entry.year));
      const content = element('div', 'record-content');
      const title = element(entry.available ? 'a' : 'span', 'record-title', entry.title);
      if (entry.available) title.href = entry.href;
      content.append(title, element('p', 'excerpt', entry.excerpt || '仅保存收藏记录，正文尚未归档。'));
      if (entry.bookmark_url) {
        const link = element('a', 'record-source', '打开原地址');
        link.href = entry.bookmark_url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.setAttribute('aria-label', `打开原地址 ${entry.title}`);
        content.append(link);
      }
      const meta = element('div', 'record-meta');
      meta.append(element('span', '', entry.source), element('span', '', entry.type === 'page' ? '空间文章' : '其他收藏'));
      content.append(meta);
      const badge = element('span', 'badge ' + (entry.available ? 'ready' : ''), entry.available ? '可离线阅读' : entry.type === 'page' ? '待归档' : '仅索引');
      row.append(check, date, content, badge); $('list').append(row);
    });
    $('section-title').textContent = labels[filter];
    $('result-count').textContent = `${visible.length} 条收藏`;
    $('empty').hidden = visible.length > 0;
    updateSelection();
  }
  document.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => {
    filter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach(node => {
      node.classList.toggle('active', node === button);
      node.setAttribute('aria-pressed', String(node === button));
    });
    render();
  }));
  $('q').addEventListener('input', render); $('year').addEventListener('change', render);
  $('select-all').addEventListener('change', () => {
    visible.filter(e => e.available).forEach(e => {
      if ($('select-all').checked) selected.add(e.key); else selected.delete(e.key);
    });
    render();
  });
  $('reset').addEventListener('click', () => {
    $('q').value = ''; $('year').value = '';
    document.querySelector('[data-filter="all"]').click();
  });
  $('export').addEventListener('click', () => {
    const chosen = data.entries.filter(e => e.available && selected.has(e.key));
    if (!chosen.length) return;
    const text = chosen.map(e => `${e.title}\n收藏于 ${e.date || '未知日期'} · ${e.source}\n\n${e.text}`).join('\n\n' + '─'.repeat(36) + '\n\n');
    const url = URL.createObjectURL(new Blob(['\ufeff', text], {type: 'text/plain;charset=utf-8'}));
    const link = element('a'); link.href = url; link.download = `百度空间_所选${chosen.length}篇.txt`;
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  render();
})();
