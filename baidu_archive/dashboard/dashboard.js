'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
 home:'M3 10 12 3l9 7v10H5V10m4 10v-7h6v7',box:'M3 8h18v13H3zM2 3h20v5H2zm7 10h6',
 article:'M6 3h9l4 4v14H5V3zm9 0v5h4M9 12h6m-6 4h6',task:'M4 7h12m-3-3 3 3-3 3M20 17H8m3-3-3 3 3 3',
 settings:'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M12 2v3m0 14v3M2 12h3m14 0h3M5 5l2 2m10 10 2 2M5 19l2-2M17 7l2-2',
 download:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5',refresh:'M20 8a8 8 0 0 0-14-3L3 8m0-5v5h5m-4 8a8 8 0 0 0 14 3l3-3m0 5v-5h-5',
 check:'M5 12l4 4L19 6',image:'M3 3h18v18H3zm0 14 6-6 4 4 3-3 5 5M16 6h.01',warning:'M12 3 2 21h20L12 3zm0 6v5m0 3h.01',
 search:'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14m5 12 6 6',arrow:'M4 12h16m-6-6 6 6-6 6',play:'m7 4 14 8-14 8z',stop:'M5 5h14v14H5z',eye:'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zm10-3a3 3 0 1 0 0 6 3 3 0 0 0 0-6',shield:'m12 2 8 3v7c0 5-8 10-8 10S4 17 4 12V5zM8 11l3 3 5-6',link:'m9 15 6-6M8 17l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2-1 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0'
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.box}"/></svg>`;
const button = (label, action, symbol, extra='') => `<button class="btn" data-action="${action}" ${extra}>${symbol?icon(symbol):''}${label}</button>`;
let library, token, job=null, currentView='overview', loading=false;
const selected = new Set();
const filters = {tab:'page', query:'', year:'', status:'', page:1};
let modalKeys=[], lastJobSignature='', toastTimer, pendingImageDownload=null;
const active = () => job && ['running','stopping'].includes(job.status);
const titles = {overview:'概览', archives:'文章归档', tasks:'任务', settings:'设置'};
const jobLabels = {completed:'已完成', partial:'部分未完成', stopped:'已停止', running:'进行中', stopping:'正在停止', failed:'未完成'};
const connectionLabels = {connected:'连接正常', saved:'登录凭证已保存', missing:'尚未连接', expired:'登录已失效', error:'连接未通过'};
function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,5000);}
async function api(path, data){
 const response=await fetch(path, data===undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Archive-Token':token},body:JSON.stringify(data)});
 const result=await response.json(); if(!response.ok)throw new Error(result.error || '操作未完成'); return result;
}
function modal(title,subtitle,body,actions,reader=false){
 const dialog=$('modal');dialog.classList.toggle('reader-modal',reader);
 $('modal-content').innerHTML=`<div class="modal-head"><div><h2 id="modal-title">${esc(title)}</h2><p>${esc(subtitle)}</p></div><button class="close" data-action="close" aria-label="关闭">×</button></div>${body}<div class="modal-actions">${actions}</div>`;
 if(!dialog.open)dialog.showModal();
}
function closeModal(){ $('modal').close();$('modal-content').replaceChildren(); }
function navigate(view){if(location.hash==='#'+view){currentView=view;render();}else location.hash=view;}
function renderNav(){
 $('navigation').innerHTML=[['overview','home'],['archives','box'],['tasks','task']].map(([name,symbol])=>`<a href="#${name}" aria-label="${titles[name]}" title="${titles[name]}" class="${currentView===name?'active':''}" ${currentView===name?'aria-current="page"':''}>${icon(symbol)}<span>${titles[name]}</span>${name==='archives'?`<small class="count">${library.summary.indexed}</small>`:''}${name==='tasks'&&active()?'<small class="count">1</small>':''}</a>`).join('');
 document.querySelector('.settings-nav').classList.toggle('active',currentView==='settings');
 $('page-title').textContent=titles[currentView]; document.title=`${titles[currentView]} · 百度空间归档`;
 $('account-state').textContent=connectionLabels[library.connection.state]||'尚未连接';
}
function stat(label,value,sub,symbol,warn=false){return `<div class="stat ${warn?'orange':''}"><span class="stat-icon">${icon(symbol)}</span><div><p class="stat-title">${label}</p><strong class="stat-number">${value}</strong><small>${sub}</small></div></div>`;}
function overview(){
 const s=library.summary, pending=s.pages-s.ready, stored=s.historical_indexed??s.indexed;
 return `<section class="section-head row between hero"><div><h2>你的百度空间，保存在这里</h2><p class="lead">连接百度账号，选择要保存的文章，随时在本地阅读或导出。</p></div><button class="btn primary" data-action="go-archives">${icon('box')}选择文章</button></section>
 ${s.legacy_images?`<div class="notice"><div><strong>${s.legacy_images} 个旧版本图片引用尚未补证</strong><p>其中 ${s.legacy_saved_images} 个文件仍可校验，尚未验证文章与资源关系。可从已保存 raw 数据离线补证；证据不足会保留原状态并报告原因。</p></div>${button('离线验证旧归档','verify-legacy','shield',active()?'disabled':'')}</div>`:''}<section class="stats" aria-label="归档统计">${stat('收藏列表',s.indexed,'本轮观察 '+s.indexed+' 条 · 历史保留 '+stored+' 条','box')}${stat('已保存正文',s.ready,pending?'还有 '+pending+' 篇待抓取':'全部空间文章已保存','article')}${stat('已下载图片',s.downloaded,'共发现 '+s.images+' 个图片引用','image')}${stat('待补齐图片',s.failed_images,s.failed_images?'原始地址保留，正文可阅读':'没有下载失败的图片','warning',!!s.failed_images)}</section>
 <section class="card"><div class="card-heading row between"><div><span class="kicker">开始归档</span><h3>三步，把收藏保存到本地</h3></div><span class="tag green">${icon('shield')} 只读访问</span></div><div class="steps">
 <div class="step"><span class="step-number">1</span><h3>连接百度账号</h3><p>${library.connection.state==='connected'?'百度连接已验证，可读取你的收藏。':library.connection.state==='saved'?'已保存本地登录凭证，抓取前可验证连接。':'登录你的百度账号，连接自己的收藏列表。'}</p>${button(library.connection.state==='connected'?'管理连接':'验证 / 连接账号','account','link')}</div>
 <div class="step"><span class="step-number">2</span><h3>读取收藏列表</h3><p>${s.indexed?'已有 '+s.indexed+' 条收藏。更新列表可以发现新内容。':'先读取收藏目录，再挑选要保存的文章。'}</p>${button(s.indexed?'更新收藏列表':'读取收藏列表','index','refresh',active()?'disabled':'')}</div>
 <div class="step"><span class="step-number">3</span><h3>选择文章，开始抓取</h3><p>按年份、状态筛选，勾选文章后抓取正文和图片。</p><button class="btn primary" data-action="go-archives">进入文章列表 ${icon('arrow')}</button></div></div></section>
 ${s.failed_images?`<div class="notice">${icon('warning')}<div><strong>${s.failed_images} 个图片引用还未保存</strong><p>涉及 ${library.records.filter(r=>r.failed).length} 篇文章。可查看详情或只重试未成功的图片。</p></div>${button('查看待补齐文章','show-failed','arrow')}</div>`:''}
 <div class="recent-grid"><section class="card"><div class="row between card-heading"><h3>最近的收藏</h3><button class="btn subtle small" data-action="go-archives">查看全部 →</button></div><ul class="summary-list">${library.records.filter(r=>r.type==='page').slice(0,4).map(r=>`<li>${icon('article')}<button class="record-title title" data-action="read" data-key="${esc(r.key)}" ${r.available?'':'disabled'}>${esc(r.title)}</button><time>${esc(r.date)}</time></li>`).join('')||'<li class="muted">读取收藏列表后，文章会出现在这里。</li>'}</ul></section>
 <section class="card quick-card"><span class="kicker">本地归档</span><h3>已保存的文章，随时带走</h3><p>在文章列表勾选内容，即可导出 Markdown、TXT 和已下载图片。导出无需连接百度。</p>${button('选择并导出','choose-export','download')}<p style="margin:18px 0 0;font-size:10px">原始网页和历史版本持续保存在本地。</p></section></div>`;
}
function filtered(){return library.records.filter(r=>(filters.tab==='all'||(filters.tab==='other'?r.type!=='page':r.type==='page'))&&(!filters.year||r.date.startsWith(filters.year))&&(!filters.query||[r.title,r.excerpt].join(' ').toLocaleLowerCase().includes(filters.query.toLocaleLowerCase()))&&(!filters.status||(filters.status==='ready'&&r.available)||(filters.status==='pending'&&r.type==='page'&&!r.available)||(filters.status==='failed'&&r.failed>0)));}
function badge(r){if(r.status==='legacy_unverified')return '<span class="tag">旧版本待补证</span>';return r.type!=='page'?'<span class="tag">仅收藏记录</span>':r.failed?'<span class="tag orange">图片待补齐</span>':r.status==='assets_partial'?'<span class="tag orange">资源待补齐</span>':r.available?'<span class="tag green">正文已保存</span>':'<span class="tag blue">待抓取正文</span>';}
function archives(){
 const years=[...new Set(library.records.map(r=>r.date.slice(0,4)).filter(Boolean))].sort().reverse();
 return `<section class="section-head row between"><div><h2>选择要保存的文章</h2><p class="lead">勾选文章 → 点击「抓取所选」→ 选择正文与图片。已保存的内容可以直接阅读或导出。</p></div>${button('更新收藏列表','index','refresh',active()?'disabled':'')}</section>
 <section class="image-export-bar" aria-label="图片 ZIP 导出"><div>${icon("image")}<div><strong>把文章里的图片单独带走</strong><p>按文章分文件夹 · 仅打包已下载图片 · 全部导出不受筛选影响</p></div></div><button class="btn" data-action="export-all-images" ${active()||!library.summary.downloaded?"disabled":""}>${icon("download")}导出全部图片</button></section><section class="card collection-card"><div class="tabs" aria-label="内容类型">${[['page','空间文章',library.summary.pages],['other','其他收藏',(library.summary.historical_indexed??library.summary.indexed)-library.summary.pages],['all','全部',library.summary.historical_indexed??library.summary.indexed]].map(([value,label,count])=>`<button class="tab ${filters.tab===value?'active':''}" data-action="tab" data-value="${value}" aria-pressed="${filters.tab===value}">${label}<span>${count}</span></button>`).join('')}</div>
 <div class="filters"><label class="search">${icon('search')}<input id="query" type="search" aria-label="搜索文章标题或摘要" placeholder="搜索文章标题或摘要…" value="${esc(filters.query)}"></label><select id="year" aria-label="按年份筛选"><option value="">全部年份</option>${years.map(y=>`<option ${filters.year===y?'selected':''}>${y}</option>`).join('')}</select><select id="status" aria-label="按归档状态筛选">${[['','全部状态'],['ready','正文已保存'],['pending','待抓取正文'],['failed','图片待补齐']].map(([value,label])=>`<option value="${value}" ${filters.status===value?'selected':''}>${label}</option>`).join('')}</select></div>
 ${filters.tab==='other'?'<div class="modal-note" style="margin-bottom:15px">这类收藏已保留标题和目录。目前支持抓取百度空间文章，其他收藏暂不支持正文抓取与导出。</div>':''}
 <div id="selection"></div><div id="records"></div></section>`;
}
function renderTable(){
 const matches=filtered(), pages=Math.max(1,Math.ceil(matches.length/25)); filters.page=Math.min(filters.page,pages);
 const rows=matches.slice((filters.page-1)*25,filters.page*25);
 $('records').innerHTML=matches.length?`<div class="table-wrap"><table><thead><tr><th class="check"><input type="checkbox" id="select-page" aria-label="选择本页空间文章"></th><th class="title-column">文章标题</th><th class="date-column">收藏日期</th><th class="status-column">归档状态</th><th class="images-column">本地图片</th><th class="action-column">阅读</th></tr></thead><tbody>${rows.map(r=>`<tr class="${selected.has(r.key)?'selected':''}"><td><input type="checkbox" class="record-check" data-key="${esc(r.key)}" aria-label="选择 ${esc(r.title)}" ${selected.has(r.key)?'checked':''} ${r.type!=='page'?'disabled title="其他收藏暂不支持正文抓取"':''}></td><td><button class="record-title" data-action="read" data-key="${esc(r.key)}" ${r.available?'':'disabled'} title="${esc(r.title)}">${esc(r.title)}</button><div class="record-sub">${esc(r.excerpt || (r.type==='page'?'来自百度空间':'其他收藏 · 仅保存目录记录'))}</div></td><td class="date-cell">${esc(r.date||'—')}</td><td>${badge(r)}</td><td class="date-cell">${r.images?`${r.downloaded} / ${r.images}`:'—'}</td><td><button class="btn subtle small" data-action="read" data-key="${esc(r.key)}" aria-label="阅读 ${esc(r.title)}" ${r.available?'':'disabled'}>${icon('eye')}</button></td></tr>`).join('')}</tbody></table></div><div class="pagination"><span>共 ${matches.length} 条 · 每页 25 条</span><div class="row">${button('上一页','previous',null,filters.page===1?'disabled':'')}<span>${filters.page} / ${pages}</span>${button('下一页','next',null,filters.page===pages?'disabled':'')}</div></div>`:`<div class="empty">${icon('search')}<h3>没有符合条件的收藏</h3><p>${library.summary.indexed?'换个关键词或重置筛选试试。':'先点击「读取收藏列表」，然后选择要抓取的文章。'}</p>${button('重置筛选','reset',null)}</div>`;
 renderSelection();
}
function renderSelection(){
 const matches=filtered(), chosen=library.records.filter(r=>selected.has(r.key)), eligible=matches.filter(r=>r.type==='page'), rows=matches.slice((filters.page-1)*25,filters.page*25).filter(r=>r.type==='page');
 const outside=chosen.filter(r=>!matches.some(m=>m.key===r.key)).length, exportable=chosen.filter(r=>r.available).length, chosenImages=chosen.reduce((sum,r)=>sum+r.downloaded,0), incompleteImages=chosen.filter(r=>r.available&&r.images>r.downloaded).length;
 $('selection').innerHTML=`<div class="selection"><div class="row wrap"><strong>已选 ${selected.size} 篇</strong>${outside?`<small>含筛选外 ${outside} 篇</small>`:''}<button class="btn subtle small" data-action="select-filtered" ${eligible.length?'':'disabled'}>选择筛选结果 ${eligible.length} 篇</button>${selected.size?'<button class="btn subtle small" data-action="clear">清空</button>':''}</div><div class="row wrap"><button class="btn" data-action="export-selected-images" ${!chosenImages||active()?"disabled":""}>${icon("image")}导出所选图片${chosenImages?" ("+chosenImages+")":""}</button>${incompleteImages?`<button class="btn subtle" data-action="assets" ${active()?'disabled':''}>仅补齐图片</button>`:''}<button class="btn" data-action="export" ${!exportable||active()?'disabled':''}>${icon('download')}导出文章${exportable?' ('+exportable+')':''}</button><button class="btn primary" data-action="archive" ${!selected.size||active()?'disabled':''}>${icon('download')}抓取所选${selected.size?' ('+selected.size+')':''}</button></div></div>`;
 const all=$('select-page');if(all){all.disabled=!rows.length;all.checked=!!rows.length&&rows.every(r=>selected.has(r.key));all.indeterminate=rows.some(r=>selected.has(r.key))&&!all.checked;}
 document.querySelectorAll('.record-check').forEach(node=>{node.checked=selected.has(node.dataset.key);node.closest('tr').classList.toggle('selected',node.checked);});
}
function verificationDetails(result){
 const labels={index_complete:'索引完整',counts_match:'数量一致',order_contiguous:'原始顺序连续',no_problem_items:'文章状态正常',pages_complete:'文章归档完成',raw_content_present:'原始响应齐全',original_content_present:'正文齐全',no_empty_content:'正文非空',no_failed_assets:'资源下载无失败',files_intact:'已保存文件校验通过'};
 const failed=Object.entries(result.checks).filter(([,ok])=>!ok).map(([name])=>labels[name]||name);
 return `<div class="job-detail verification"><div>接口声明 expected<strong>${esc(result.expected??'未知')}</strong></div><div>已索引 indexed<strong>${esc(result.indexed)}</strong></div><div>缺失 missing<strong>${esc(result.missing??'未知')}</strong></div></div>${failed.length?`<p class="lead">未通过：${failed.map(esc).join('、')}</p>`:''}`;
}
function jobCard(){
 if(!job)return `<div class="empty">${icon('task')}<h3>还没有从操作台启动任务</h3><p>在文章归档中选择内容后，抓取进度会显示在这里。</p><button class="btn primary" data-action="go-archives">选择要抓取的文章</button></div>`;
 const percent=job.total?Math.min(100,job.done/job.total*100):0;
 return `<div class="row between card-heading"><div><span class="kicker">${esc(job.phase)}</span><h3>${esc(job.label)}</h3></div><span class="tag ${job.status==='completed'?'green':active()?'blue':'orange'}">${jobLabels[job.status]||esc(job.status)}</span></div><p class="lead">${esc(job.message)}</p>
 ${job.kind==='verify'&&job.result?verificationDetails(job.result):''}
 ${job.legacy_evidence?`<p>离线补证：验证 ${job.legacy_evidence.verified} 个，仍未验证 ${job.legacy_evidence.legacy_unverified} 个。</p>${job.legacy_evidence.reasons.map(r=>`<p>${esc(r.key)}：${esc(r.reason)}。已保留旧文件，请检查保存的 raw / 正文 / 资源证据。</p>`).join('')}`:''}
 ${active()?`<div class="progress-track" role="progressbar" aria-label="当前任务进度" ${job.total&&job.done?'aria-valuenow="'+Math.round(percent)+'" aria-valuemin="0" aria-valuemax="100"':''}><div class="progress-fill ${job.done?'':'busy'}" style="width:${percent}%"></div></div>`:'<div style="height:20px"></div>'}
 <div class="job-detail"><div>任务范围<strong>${job.args.keys.length?job.args.keys.length+' 篇文章':'账号收藏列表 / 本地归档'}</strong></div><div>本轮阶段<strong>${esc(job.phase)}</strong></div>${job.total&&job.done?`<div>已处理<strong>${job.done} / ${job.total}</strong></div>`:''}</div>
 <div class="row wrap">${active()?button(job.status==='stopping'?'正在停止…':'停止任务','stop','stop',job.status==='stopping'?'disabled':''):
 !job.download&&['stopped','failed','partial'].includes(job.status)?'<button class="btn primary" data-action="resume">'+icon('play')+(job.status==='stopped'?'继续任务':'重试未完成部分')+'</button>':''}
 ${job.download?`<a class="btn primary" href="${esc(job.download)}" download>${icon('download')}下载导出包</a>`:''}${button('返回文章列表','go-archives','box')}</div>
 <details style="margin-top:16px"><summary>查看任务明细（${job.events.length} 条）</summary><div class="event-list">${job.events.map(e=>`<div class="event"><time>${esc(e.at.slice(11,19))}</time><span>${esc(e.message)}</span></div>`).join('')||'<p class="muted">任务尚无明细。</p>'}</div></details>`;
}
function tasks(){return `<section class="section-head row between"><div><h2>任务与进度</h2><p class="lead">抓取和导出在后台进行。停止后保留进度，可以继续处理未完成的内容。</p></div>${button('校验本地归档','verify','shield',active()?'disabled':'')}</section><section class="card" id="job-panel">${jobCard()}</section><section class="card"><h3>最近运行记录</h3><ul class="summary-list">${library.history.map(r=>`<li>${icon('task')}<span>${esc(({index:'读取收藏列表',fetch:'抓取正文',parse:'解析正文',assets:'下载图片','ui:index':'更新收藏列表','ui:archive':'抓取所选文章','ui:export':'导出所选文章','ui:export_images':'导出文章图片','ui:verify':'校验归档','ui:connection':'验证连接','ui:assets':'补齐图片'})[r.command]||r.command)}</span><span class="tag">${({completed:'已结束',partial:'部分未完成',stopped:'已停止',failed:'未完成',auth_error:'登录失效',interrupted:'已中断'})[r.outcome]||'已结束'}</span>${r.download?`<a class="btn small" href="${esc(r.download)}" download>下载 ZIP</a>`:''}<time>${esc(new Date(r.started_at).toLocaleString('zh-CN',{hour12:false}))}</time></li>`).join('')||'<li class="muted">暂无运行记录</li>'}</ul></section>`;}
function settings(){const s=library.settings;return `<section class="section-head"><h2>按你的方式归档</h2><p class="lead">设置连接和抓取速度。新的速度设置在下一次任务开始时生效。</p></section><div class="settings-grid"><section class="card"><h3>抓取速度</h3><label class="field">同时请求数<input id="concurrency" type="number" min="1" max="8" step="1" value="${s.concurrency}"><small>同时处理多少个请求，建议 4。</small></label><label class="field">请求间隔（秒）<input id="delay" type="number" min="0.1" max="10" step="0.05" value="${s.delay}"><small>两次请求之间的最短间隔，当前提速配置为 0.25 秒。</small></label><label class="field">列表每批条数<input id="page-limit" type="number" min="1" max="100" step="1" value="${s.page_limit}"><small>每次读取最多 100 条收藏。文章正文需要逐篇请求。</small></label><button class="btn primary" data-action="save-settings" ${active()?'disabled':''}>保存设置</button>${active()?'<p class="lead" style="margin-top:12px">当前任务结束后可修改。</p>':''}</section><div><section class="card"><h3>账号连接</h3><p class="lead">${connectionLabels[library.connection.state]}</p><div style="margin-top:18px">${button('管理百度连接','account','link')}</div></section><section class="card"><h3>本地保存位置</h3><p class="lead">原始网页、文章、图片和任务进度保存在这里。</p><p class="path" style="margin:18px 0">${esc(s.directory)}</p><small class="muted">重新抓取会保留历史版本。导出包在 exports 文件夹。</small></section><section class="card"><h3>目前支持的内容</h3><ul class="help-list"><li>百度空间文章正文和文中图片</li><li>全部收藏的标题、日期与原始顺序</li><li>其他收藏、外链与旧媒体嵌入保留记录</li><li>按文章选择导出 Markdown、TXT 和图片</li></ul></section></div></div>`;}
function render(){if(!library)return;renderNav();$('main').innerHTML=({overview,archives,tasks,settings}[currentView])();if(currentView==='archives')renderTable();}
async function reload(){library=await api('/api/library');render();}
async function startTask(args){
 job=await api('/api/jobs',args);if(args.kind==='export_images')pendingImageDownload=job.id;closeModal();navigate('tasks');if(currentView==='tasks')render();toast('任务已开始，可在这里查看进度或停止。');
}
function archiveModal(keys){
 modalKeys=[...keys];const ready=library.records.filter(r=>keys.includes(r.key)&&r.available).length;
 modal('抓取 '+keys.length+' 篇文章','确认范围与保存内容，再开始任务。',`<div class="modal-body"><p class="modal-label">保存内容</p><label class="option"><input type="radio" name="content" value="images" checked><span><strong>正文与图片</strong><small>保存文章正文，并把可用的图片下载到本地。</small></span></label><label class="option"><input type="radio" name="content" value="text"><span><strong>仅正文</strong><small>先保存文字与原始网页，稍后可以补齐图片。</small></span></label><p class="modal-label" style="margin-top:23px">处理方式</p><label class="option"><input type="radio" name="mode" value="resume" checked><span><strong>继续归档（推荐）</strong><small>跳过已经完成的内容，只处理待抓取与未完成的部分。</small></span></label><label class="option"><input type="radio" name="mode" value="refresh"><span><strong>重新抓取所选文章</strong><small>获取新的正文版本，已经保存的原件和历史版本会保留。</small></span></label><p class="modal-note">当前选择 ${keys.length} 篇，其中 ${ready} 篇已保存正文。${ready===keys.length?'使用「继续归档」时，已完成的文章会直接跳过。':''}</p><div class="error-inline" id="modal-error"></div></div>`,`${button('取消','close')}<button class="btn primary" data-action="confirm-archive">${icon('download')}开始抓取 ${keys.length} 篇</button>`);
}
function accountModal(){const state=library.connection.state;modal('连接百度账号','归档工具只读取你本人的收藏。',`<div class="modal-body"><div class="login-status"><strong>${connectionLabels[state]}</strong><p>${state==='connected'?'连接已验证，可以更新收藏列表和抓取文章。':state==='saved'?'本地已有登录凭证，点击下方按钮即可检查是否仍然有效。':'如登录已失效，请先在百度完成登录，再更新本地凭证。'}</p></div><div class="row wrap">${button('验证当前连接','check-connection','refresh',active()?'disabled':'')}<a class="btn" href="https://wenzhang.baidu.com/" target="_blank" rel="noopener noreferrer">在百度打开收藏 ↗</a></div><details style="margin-top:20px"><summary>登录失效时更新凭证</summary><p class="muted" style="font-size:12px;margin-top:12px">在百度网页使用同一个账号完成登录，打开浏览器开发者工具的 Network，复制 <b>/fav/list</b> 请求为 cURL，粘贴到下面。工具仅在本机保存凭证。</p><textarea id="session-input" aria-label="百度收藏列表请求 cURL" placeholder="粘贴收藏列表请求的 cURL…" autocomplete="off" spellcheck="false"></textarea><button class="btn" data-action="save-session" style="margin-top:10px" ${active()?'disabled':''}>保存并验证</button></details><div class="error-inline" id="modal-error"></div></div>`,button('关闭','close'));}
async function action(name,node){
 switch(name){
 case 'collapse-sidebar':{const collapsed=document.body.classList.toggle('sidebar-collapsed');node.setAttribute('aria-expanded',String(!collapsed));node.setAttribute('aria-label',collapsed?'展开侧栏':'收起侧栏');break;}
 case 'export-all-images':await startTask({kind:'export_images',scope:'all'});break;
 case 'export-selected-images':await startTask({kind:'export_images',scope:'selected',keys:[...selected]});break;
 case 'close':closeModal();break;
 case 'account':accountModal();break;
 case 'go-archives':navigate('archives');break;
 case 'choose-export':navigate('archives');toast('勾选要带走的文章，然后点击「导出文章」或「导出所选图片」。');break;
 case 'index':await startTask({kind:'index'});break;
 case 'check-connection':await startTask({kind:'connection'});break;
 case 'save-session':await api('/api/session',{curl:$('session-input').value});$('session-input').value='';await startTask({kind:'connection'});break;
 case 'show-failed':filters.tab='page';filters.status='failed';filters.query='';filters.year='';filters.page=1;navigate('archives');break;
 case 'tab':filters.tab=node.dataset.value;filters.page=1;render();break;
 case 'select-filtered':filtered().filter(r=>r.type==='page').forEach(r=>selected.add(r.key));renderSelection();break;
 case 'clear':selected.clear();renderSelection();break;
 case 'reset':Object.assign(filters,{query:'',year:'',status:'',page:1});render();break;
 case 'previous':filters.page--;renderTable();break;
 case 'next':filters.page++;renderTable();break;
 case 'archive':if(selected.size)archiveModal([...selected]);break;
 case 'confirm-archive':await startTask({kind:'archive',keys:modalKeys,images:document.querySelector('[name=content]:checked').value==='images',force:document.querySelector('[name=mode]:checked').value==='refresh'});break;
 case 'export':{
  modalKeys=library.records.filter(r=>selected.has(r.key)&&r.available).map(r=>r.key);
  modal('导出 '+modalKeys.length+' 篇文章','保存为一个可携带的 ZIP 文件。',`<div class="modal-body"><p>包含 <b>Markdown、TXT、已下载图片</b>和文章信息，并附文件校验清单。</p><p class="modal-note">仅导出已保存正文的 ${modalKeys.length} 篇文章。${selected.size>modalKeys.length?'选择中尚未保存的文章不会被导出。':''}未成功下载的图片保留原始地址。</p><div class="error-inline" id="modal-error"></div></div>`,`${button('取消','close')}<button class="btn primary" data-action="confirm-export">${icon('download')}生成导出包</button>`);break;}
 case 'confirm-export':await startTask({kind:'export',keys:modalKeys});break;
 case 'assets':modalKeys=library.records.filter(r=>selected.has(r.key)&&r.available&&r.images>r.downloaded).map(r=>r.key);modal('补齐所选文章的图片','只处理尚未成功保存的图片。',`<div class="modal-body"><p>将尝试补齐 ${modalKeys.length} 篇文章的图片，已经保存的正文和图片会跳过。</p><div class="error-inline" id="modal-error"></div></div>`,`${button('取消','close')}<button class="btn primary" data-action="confirm-assets">开始补齐图片</button>`);break;
 case 'confirm-assets':await startTask({kind:'assets',keys:modalKeys});break;
 case 'verify-legacy':await startTask({kind:'verify',rebuild_legacy:true});break;
 case 'verify':await startTask({kind:'verify'});break;
 case 'stop':job=await api('/api/stop',{});if(currentView==='tasks')$('job-panel').innerHTML=jobCard();break;
 case 'resume':await startTask({...job.args,force:false});break;
 case 'save-settings':library.settings=await api('/api/settings',{concurrency:Number($('concurrency').value),delay:Number($('delay').value),page_limit:Number($('page-limit').value)});toast('设置已保存，下一次任务将使用新的速度。');break;
 case 'read':{const r=library.records.find(r=>r.key===node.dataset.key);if(!r?.available)break;modal(r.title,'本地归档 · '+(r.date||'未知日期'),`<iframe title="文章正文" sandbox="allow-same-origin" src="/article?key=${encodeURIComponent(r.key)}"></iframe>`,'',true);break;}
 }
}
document.addEventListener('click',async event=>{
 const node=event.target.closest('[data-action]');if(!node||node.disabled)return;
 const wasDisabled=node.disabled;node.disabled=true;
 try{await action(node.dataset.action,node);}catch(error){if($('modal-error'))$('modal-error').textContent=error.message;else toast(error.message);}finally{if(node.isConnected)node.disabled=wasDisabled;}
});
document.addEventListener('input',event=>{if(event.target.id==='query'){filters.query=event.target.value;filters.page=1;renderTable();}});
document.addEventListener('change',event=>{
 const target=event.target;
 if(target.id==='year'||target.id==='status'){filters[target.id]=target.value;filters.page=1;renderTable();}
 if(target.classList.contains('record-check')){target.checked?selected.add(target.dataset.key):selected.delete(target.dataset.key);renderSelection();}
 if(target.id==='select-page'){filtered().slice((filters.page-1)*25,filters.page*25).filter(r=>r.type==='page').forEach(r=>target.checked?selected.add(r.key):selected.delete(r.key));renderSelection();}
});
window.addEventListener('hashchange',()=>{currentView=titles[location.hash.slice(1)]?location.hash.slice(1):'overview';render();});
async function poll(){
 if(loading)return;loading=true;
 try{job=await api('/api/job');const signature=job?job.id+':'+job.status:'';
  if(job?.id===pendingImageDownload && ['completed','partial'].includes(job.status) && job.download){pendingImageDownload=null;const link=document.createElement('a');link.href=job.download;link.download='';document.body.append(link);link.click();link.remove();toast(job.status==='partial'?'已生成部分图片 ZIP。未纳入的图片已列入清单。':'图片 ZIP 已生成。若未自动下载，请点击「下载导出包」。');}
  if(signature!==lastJobSignature){lastJobSignature=signature;library=await api('/api/library');render();}
  else if(currentView==='tasks'&&job&&active()){const expanded=$('job-panel').querySelector('details')?.open; $('job-panel').innerHTML=jobCard(); if(expanded)$('job-panel').querySelector('details').open=true;}
 }catch{if(active())toast('与本地服务的连接中断，重新连接后可查看保存进度。');}finally{loading=false;}
}
(async()=>{try{token=(await api('/api/bootstrap')).token;job=await api('/api/job');lastJobSignature=job?job.id+':'+job.status:'';currentView=titles[location.hash.slice(1)]?location.hash.slice(1):'overview';await reload();setInterval(poll,1500);}catch(error){$('main').innerHTML=`<div class="screen-error">无法连接归档服务：${esc(error.message)}。请启动本地操作台后刷新。</div>`;}})();
