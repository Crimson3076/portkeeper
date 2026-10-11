const fs = require('fs');
const path = require('path');
const assert = require('assert');
const {JSDOM} = require(process.env.PORTKEEPER_JSDOM || 'jsdom');
const base = path.resolve(__dirname, '..');
const stylesheet = fs.readFileSync(path.join(base, 'static/styles.css'),'utf8');
assert(stylesheet.startsWith(':root'));
assert(!stylesheet.includes('const projectPageElement'));
assert(stylesheet.includes('.project-grid.layout-workspace'));
assert(stylesheet.includes('.project-grid.layout-collection'));
assert(stylesheet.includes('.project-grid.layout-list'));
const id = n => String(n).padStart(32, '0');
const project = (n,name,prefix,profile=null) => ({id:id(n),name,repo:'example/game',targets:['linux','windows'],profile,recipe:{mode:profile?'profile':'capture'},status:'archived',metadata:{filename_prefix:prefix,igdb_id:n===3?2:n,romm_rom_id:100+n,game:name,cover_policy:'romm'},poll_minutes:360});
const fixture = {projects:[project(1,'Ship of Harkinian','Ship', 'shipwright'),project(2,'Bloodborne','Bloodborne'),project(3,'Bloodborne Windows','Bloodborne')],artifacts:[{id:id(4),project_id:id(1),version:'9.3',target:'linux',sha256:'build-signature',validation_level:'launch_checked',created_at:'2026-10-11T12:00:00Z'}],jobs:[],notifications:[],summary:{projects:3,artifacts:1},settings:{},runner:{},romm:{url:'https://romm.example',has_token:true,configured:true,connected:true},assistant:{}};
const imports = prefix => ['linux','win'].map((platform,n) => ({id:200+n+(prefix==='Bloodborne'?10:0),name:prefix+' portable',platform_slug:platform,summary:prefix==='Bloodborne'&&platform==='linux'?'Experimental: Linux startup failed.':'Testing notes retained',package_signature:prefix+'-'+platform,files:[{name:prefix+'-v1-'+platform+'.zip'}]}));
async function create(saved, storageBlocked=false, source=fixture) {
  const errors=[];
  const dom=new JSDOM(fs.readFileSync(path.join(base,'static/index.html'),'utf8'),{url:'http://localhost:8787',runScripts:'outside-only'});
  const w=dom.window;w.scrollTo=()=>{};w.setInterval=()=>0;w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;};
  if(saved)w.localStorage.setItem('portkeeper.library-layout',saved);
  if(storageBlocked)Object.defineProperty(w,'localStorage',{get(){throw new Error('Storage unavailable');}});
  w.addEventListener('error',e=>errors.push(e.error));
  w.fetch=async (endpoint, options={})=>{
    let payload={};
    if(endpoint==='/api/state')payload=source;
    else if(endpoint.endsWith('/playtest')){const body=JSON.parse(options.body);const game=source.projects.find(p=>endpoint.includes(p.id));payload={id:'test-record',kind:'test',project_id:game.id,project_name:game.name,target:'linux',package:{kind:'romm',romm_rom_id:body.romm_rom_id,signature:body.signature},result:body.result,device:body.device,notes:body.notes,created_at:'2026-10-11T15:00:00Z'};source.playtests=[payload];source.activity=[payload];}
    else if(endpoint.match(/^\/api\/projects\/[^/]+\/imports$/)){const game=source.projects.find(p=>endpoint.includes(p.id));payload={items:imports(game.metadata.filename_prefix)};}
    else if(endpoint.startsWith('/api/romm/search?')){const prefix=new URL(endpoint,w.location.href).searchParams.get('q');payload={items:imports(prefix)};}
    else if(endpoint.startsWith('/preservation',0)||endpoint.includes('/preservation'))payload={};
    else if(endpoint==='/api/profiles')payload={profiles:[]};
    else if(endpoint==='/api/recipes')payload={recipes:[]};
    return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(payload))};
  };
  w.eval(fs.readFileSync(path.join(base,'static/app.js'),'utf8'));
  await new Promise(resolve=>setTimeout(resolve,30));
  assert.equal(errors.length,0,errors.map(e=>e.stack).join('\n'));
  return dom;
}
async function tick(){await new Promise(resolve=>setTimeout(resolve,15));}
(async()=>{
  let dom=await create();let w=dom.window,d=w.document;
  assert(d.getElementById('project-grid').classList.contains('layout-workspace'));
  assert.equal(d.querySelectorAll('.workspace-game-button').length,2,'Platform projects must be grouped');
  assert.equal(d.getElementById('summary-projects').textContent,'2');
  assert.equal(d.getElementById('summary-artifacts').textContent,'4','Platform downloads must not double count archives and RomM imports');
  assert.equal(d.getElementById('project-page').parentElement.className,'workspace-game-main');
  assert(d.querySelector('.downloads-card').textContent.includes('Open in RomM'));
  assert.equal(d.querySelectorAll('.download-platform-row').length,2);
  const blood=[...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne'));
  blood.click();await tick();assert(d.getElementById('project-heading').textContent.includes('Bloodborne'));
  assert.equal(w.localStorage.getItem('portkeeper.selected-game'),id(2));
  assert(d.querySelector('.downloads-card').textContent.includes('startup failed'));
  [...d.querySelectorAll('.downloads-card button')].find(n=>n.textContent==='Record test result').click();
  assert(d.getElementById('validation-dialog').open);
  const result=d.getElementById('validation-result');result.value='failed';result.dispatchEvent(new w.Event('change'));
  assert(d.getElementById('validation-device').required);assert(d.getElementById('validation-notes').required);assert(d.getElementById('validation-confirm').required);
  result.value='untested';result.dispatchEvent(new w.Event('change'));assert(!d.getElementById('validation-device').required);
  result.value='failed';result.dispatchEvent(new w.Event('change'));
  d.getElementById('validation-device').value='Test laptop';d.getElementById('validation-notes').value='Black screen';d.getElementById('validation-confirm').checked=true;
  d.getElementById('validation-form').dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();
  assert(!d.getElementById('validation-dialog').open);assert(d.querySelector('.downloads-card').textContent.includes('Doesn’t work'));
  w.location.hash='activity';await tick();assert(d.getElementById('activity-content').textContent.includes('Black screen'));
  w.location.hash='library';await tick();
  d.getElementById('project-versions-tab').click();await tick();assert.equal(d.getElementById('project-content').getAttribute('aria-labelledby'),'project-versions-tab');
  w.location.hash='settings';await tick();
  const selectLayout=value=>{const radio=d.querySelector(`[name="library-layout"][value="${value}"]`);radio.checked=true;radio.dispatchEvent(new w.Event('change',{bubbles:true}));};
  selectLayout('collection');assert.equal(w.localStorage.getItem('portkeeper.library-layout'),'collection');
  w.location.hash='library';await tick();assert(d.getElementById('project-grid').classList.contains('layout-collection'));assert.equal(d.querySelectorAll('#project-grid .project-card').length,2);
  w.location.hash='settings';await tick();selectLayout('list');w.location.hash='library';await tick();assert(d.getElementById('project-grid').classList.contains('layout-list'));
  const stage=d.getElementById('library-stage');stage.value='attention';stage.dispatchEvent(new w.Event('change',{bubbles:true}));assert.equal(d.querySelectorAll('#project-grid .project-card').length,1);
  dom.window.close();
  for(const value of ['collection','list','invalid']){dom=await create(value);assert(dom.window.document.getElementById('project-grid').classList.contains('layout-'+(value==='invalid'?'workspace':value)));dom.window.close();}
  dom=await create(null,true);assert(dom.window.document.getElementById('project-grid').classList.contains('layout-workspace'));dom.window.close();
  dom=await create(null,false,{...fixture,projects:[],artifacts:[]});assert(dom.window.document.getElementById('project-grid').textContent.includes('first game'));dom.window.close();
  console.log('UI layouts passed: default workspace, saved preferences, collection/list switching, grouped platforms, RomM imports, correct counts, game selection, deep links, filters, empty state and blocked storage.');
})().catch(error=>{console.error(error);process.exitCode=1;});
