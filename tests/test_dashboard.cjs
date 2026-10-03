const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const test = require('node:test');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../baidu_archive/dashboard/dashboard.js'), 'utf8');
function render(result) {
  const context = vm.createContext({});
  vm.runInContext(source.slice(0, source.indexOf("document.addEventListener('click'")), context);
  return vm.runInContext(`job={kind:'verify',status:'partial',phase:'已结束',label:'校验归档',message:'校验未通过',total:0,done:0,args:{keys:[]},events:[],result:${JSON.stringify(result)}};jobCard()`, context);
}
test('verification displays expected / indexed / missing and failed checks', () => {
  const html = render({expected:2,indexed:1,missing:1,checks:{index_complete:false,files_intact:true}});
  assert.match(html, /expected<strong>2<\/strong>/);
  assert.match(html, /indexed<strong>1<\/strong>/);
  assert.match(html, /missing<strong>1<\/strong>/);
  assert.match(html, /未通过：索引完整/);
});
test('unconfirmed API count remains unknown', () => {
  assert.match(render({expected:null,indexed:0,missing:null,checks:{index_complete:false}}), /missing<strong>未知<\/strong>/);
});

test('historical rows do not create negative counts when current index is partial', () => {
  const context = vm.createContext({});
  vm.runInContext(source.slice(0, source.indexOf("document.addEventListener('click'")), context);
  const html = vm.runInContext(`library={summary:{indexed:1,historical_indexed:3,pages:3,ready:3,downloaded:0,images:0,failed_images:0},records:[],connection:{state:'saved'}};overview()`, context);
  assert.match(html, /本轮观察 1 条 · 历史保留 3 条/);
  assert.doesNotMatch(html, /其他 -2 条/);
});
test('partial non-image media does not receive a complete badge', () => {
  const context = vm.createContext({});
  vm.runInContext(source.slice(0, source.indexOf("document.addEventListener('click'")), context);
  const html = vm.runInContext(`badge({type:'page',failed:0,available:true,status:'assets_partial'})`, context);
  assert.match(html, /资源待补齐/);
});
test('legacy saved files are shown as evidence pending with an offline action', () => {
  const context = vm.createContext({});
  vm.runInContext(source.slice(0, source.indexOf("document.addEventListener('click'")), context);
  const html = vm.runInContext(`library={summary:{indexed:1,historical_indexed:1,pages:1,ready:1,downloaded:0,images:1,failed_images:0,legacy_images:1,legacy_saved_images:1},records:[],connection:{state:'saved'}};overview()`, context);
  assert.match(html, /文件仍可校验/);
  assert.match(html, /data-action="verify-legacy"/);
  assert.doesNotMatch(html, /图片引用还未保存/);
  assert.match(vm.runInContext(`badge({type:'page',available:true,status:'legacy_unverified'})`,context), /旧版本待补证/);
});
