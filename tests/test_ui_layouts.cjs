process.env.TZ = 'America/New_York';
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
const requests=[];
async function create(saved, storageBlocked=false, source=fixture, prefs={}) {
  const errors=[];
  const dom=new JSDOM(fs.readFileSync(path.join(base,'static/index.html'),'utf8'),{url:'http://localhost:8787',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window;w.scrollTo=()=>{};w.setInterval=callback=>{w.pollRefresh=callback;return 0;};w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;};
  if(saved)w.localStorage.setItem('portkeeper.library-layout',saved);
  Object.entries(prefs).forEach(([key,value])=>w.localStorage.setItem(key,value));
  if(storageBlocked)Object.defineProperty(w,'localStorage',{get(){throw new Error('Storage unavailable');}});
  w.addEventListener('error',e=>errors.push(e.error));
  w.fetch=async (endpoint, options={})=>{
    let payload={};
    if(endpoint==='/api/state')payload=source;
    else if(endpoint==='/api/romm/matching'||endpoint==='/api/romm/match-now'){
      const body=JSON.parse(options.body);requests.push({endpoint,method:options.method,body});
      if(source.match_error)return {ok:false,status:400,json:async()=>({error:source.match_error})};
      if(endpoint==='/api/romm/matching'){
        Object.assign(source.romm.matching,{mode:body.mode,reason:'Fixed interval: every 5 minutes'});
        payload=source.romm.matching;
      }else{
        Object.assign(source.romm.matching,{last_check:'2026-10-29T12:00:00Z',next_check:'2026-10-29T12:05:00Z',pending:['Game.zip']});
        payload={matched:2,matching:source.romm.matching};
      }
    }
    else if(endpoint==='/api/version'){payload=source.version_info||{version:'0.3.0',commit:null};}
    else if(endpoint==='/api/assistant/models'){const body=JSON.parse(options.body);requests.push({endpoint,body});payload={models:source.agent_model_choices?.[body.provider] || ['provider-model-a','provider-model-b']};}
    else if(endpoint==='/api/assistant/provider'){
      const body=JSON.parse(options.body);requests.push({endpoint,body});source.assistant||={};source.assistant.provider=body.provider;
      if(['codex','claude_code','gemini'].includes(body.provider)){source.assistant.agent_models||={};source.assistant.agent_models[body.provider]=body.model;source.assistant.agent_model=body.model;source.assistant.configured=Boolean(source.assistant.agents?.[body.provider]?.authenticated);}
      source.assistant.drafting_model=body.model || 'provider default';payload=source.assistant;
    }
    else if(endpoint==='/api/assistant/test'){requests.push({endpoint,body:JSON.parse(options.body)});payload={model:source.assistant.drafting_model,seconds:1};}
    else if(endpoint==='/api/assistant/agent/login/input'){
      const body=JSON.parse(options.body);requests.push({endpoint,body});payload=source.agent_login_input_result || {provider:body.provider,status:'connected',message:'Native sign-in complete'};
      source.agent_logins||={};source.agent_logins[body.provider]=payload;if(payload.status==='connected'){source.assistant.agents[body.provider].authenticated=true;if(source.assistant.provider===body.provider)source.assistant.configured=true;}
    }
    else if(endpoint==='/api/assistant/agent/login'){
      const body=JSON.parse(options.body);requests.push({endpoint,body});payload=source.agent_login_start?.[body.provider] || {provider:body.provider,status:'waiting',message:'Complete the native sign-in',url:'https://auth.example.test/device',code:'ONE-TIME-CODE'};
      source.agent_logins||={};source.agent_logins[body.provider]=payload;
    }
    else if(endpoint.startsWith('/api/assistant/agent/login?')){
      requests.push({endpoint});const provider=new URL(endpoint,w.location.href).searchParams.get('provider');payload=source.agent_logins?.[provider] || {provider,status:'not_started'};
      if(payload.status==='connected'){source.assistant.agents[provider].authenticated=true;if(source.assistant.provider===provider)source.assistant.configured=true;}
    }
    else if(endpoint==='/api/assistant/agent/logout'){
      const body=JSON.parse(options.body);requests.push({endpoint,body});source.assistant.agents[body.provider].authenticated=false;delete source.agent_logins?.[body.provider];if(source.assistant.provider===body.provider)source.assistant.configured=false;payload={provider:body.provider,status:'not_started'};
    }
    else if(endpoint.endsWith('/playtest')){const body=JSON.parse(options.body);const game=source.projects.find(p=>endpoint.includes(p.id));payload={id:'test-record',kind:'test',project_id:game.id,project_name:game.name,target:'linux',package:{kind:'romm',romm_rom_id:body.romm_rom_id,signature:body.signature},result:body.result,device:body.device,notes:body.notes,created_at:'2026-10-11T15:00:00Z'};source.playtests=[payload];source.activity=[payload];}
    else if(endpoint.match(/^\/api\/projects\/[^/]+\/imports$/)){const game=source.projects.find(p=>endpoint.includes(p.id));payload={items:source.no_imports?[]:imports(game.metadata.filename_prefix)};}
    else if(endpoint.startsWith('/api/romm/search?')){const prefix=new URL(endpoint,w.location.href).searchParams.get('q');payload={items:imports(prefix)};}
    else if(endpoint.startsWith('/preservation',0)||endpoint.includes('/preservation'))payload={};
    else if(endpoint==='/api/profiles')payload={profiles:[]};
    else if(endpoint==='/api/recipes')payload={recipes:[]};
    else if(endpoint.startsWith('/api/github/lookup?'))payload=source.platform_lookup || {repository:'example/game',version:'v1',assets:['game-linux.zip','game-windows.zip'],platforms:['linux','windows'],platform_detection:{suggested_targets:['linux','windows'],platforms:[{id:'linux',label:'Linux',packaging_supported:true,evidence:[{kind:'release',detail:'game-linux.zip',source:'Release files'}]},{id:'windows',label:'Windows',packaging_supported:true,evidence:[{kind:'documentation',detail:'Windows build instructions',source:'README.md'}]},{id:'android',label:'Android',packaging_supported:false,evidence:[{kind:'documentation',detail:'Android build instructions',source:'README.md'}]}]}};
    else if(endpoint==='/api/catalog/games')payload={games:source.catalog_games || [{id:'banjo-recompiled',name:'Banjo Recompiled',targets:['linux','windows'],runner:'operations',needs:[{label:'Banjo-Kazooie (USA) 1.0 N64 ROM'}],first_run:{mode:'preloaded',text:'Your ROM is placed where Banjo looks for it.'},in_library:null}],romm_connected:true};
    else if(endpoint==='/api/discovery'||endpoint==='/api/discovery/refresh'){requests.push({endpoint,body:options.body?JSON.parse(options.body):null});payload=source.discovery || {entries:[],stale:false,refreshing:false};}
    else if(endpoint.startsWith('/api/discovery/games?')){requests.push({endpoint});const q=new URL(endpoint,w.location.href).searchParams;if(source.games_gate)await source.games_gate(q);const all=source.discovery_games||[];const text=(q.get('q')||'').toLowerCase();const items=all.filter(i=>(!text||i.name.toLowerCase().includes(text))&&(!q.get('origin')||i.origin===q.get('origin')));payload={items,total:items.length,origins:[...new Set(all.map(i=>i.origin))],systems:['windows','linux','macos'],genres:['Adventure'],count:all.length};}
    else if(endpoint.match(/^\/api\/discovery\/games\/[^/]+\/romm$/)){requests.push({endpoint});payload={checked:true,owned:true,title:'Banjo-Kazooie',matches:[{id:3,name:'Banjo-Kazooie',platform:'N64'}]};}
    else if(endpoint==='/api/discovery/resolve'){requests.push({endpoint,body:JSON.parse(options.body)});payload=source.discovery_resolution || {url:'https://recomplauncher.com/games/new-port',repositories:[{repository:'example/new-port',url:'https://github.com/example/new-port'}]};}
    else if(endpoint==='/api/catalog/games/banjo-recompiled/files')payload={inputs:[{id:'rom',label:'Banjo-Kazooie (USA) 1.0 N64 ROM',kind:'file',required:true,accepted:['USA 1.0'],found:[{path:'n64/roms/Banjo-Kazooie (USA).z64',name:'Banjo-Kazooie (USA).z64',accepted:'USA 1.0'}],rejected:[]}]};
    else if(endpoint.endsWith('/configuration/validate')){requests.push({endpoint,body:JSON.parse(options.body)});payload={valid:true};}
    else if(endpoint.match(/^\/api\/projects\/[^/]+\/inputs$/)){
      if(options.method==='POST'){const body=JSON.parse(options.body);requests.push({endpoint,body});const game=source.projects.find(p=>endpoint.includes(p.id));game.input_bindings||={};game.input_bindings[body.input]={source:body.source||'inputs',path:body.path,ps4:{title_id:'CUSA01623',app_version:body.input==='game_pkg'?'01.00':'01.02',role:body.input==='game_pkg'?'base':'update'}};payload=game;}
      else payload=source.input_choices||{inputs:[]};
    }
    else if(endpoint.startsWith('/api/catalog/definitions/'))payload=source.catalog_definition||{};
    else if(endpoint.endsWith('/draft-prompt')){requests.push({endpoint,body:JSON.parse(options.body)});payload={prompt:'Fixture prompt'};}
    else if(endpoint.endsWith('/draft-record'))payload=source.draft_record||{attempts:[]};
    else if(endpoint.endsWith('/automation-record'))payload=source.automation_record||{attempts:[],worker_runs:[],model_calls:0,worker_seconds:0};
    else if(endpoint.startsWith('/api/automation/inputs'))payload={directory:'',parent:null,items:[{name:'owned.bin',path:'owned.bin',kind:'file'}],truncated:false};
    else if(endpoint==='/api/automation/broker/pair'){requests.push({endpoint,body:JSON.parse(options.body)});payload={worker:{ready:true}};}
    else if(endpoint.endsWith('/automate')||endpoint.endsWith('/script-recipe')||endpoint.endsWith('/reply-automation')||endpoint==='/api/automation/config'){
      const body=JSON.parse(options.body);requests.push({endpoint,body});payload={job:{id:id(501)}};
    }
    else if(endpoint.endsWith('/draft') && options.method==='POST' && source.draft_error){return {ok:false,status:400,json:async()=>({error:source.draft_error})};}
    else if(endpoint.endsWith('/draft') && options.method==='POST'){requests.push({endpoint,body:JSON.parse(options.body)});payload={job:{}};}
    else if(endpoint.endsWith('/investigate') && options.method==='POST'){requests.push({endpoint,body:JSON.parse(options.body)});payload={job:{}};}
    else if(options.method==='PATCH'){const body=JSON.parse(options.body);requests.push({endpoint,body});const game=source.projects.find(p=>endpoint.includes(p.id));Object.assign(game,body);payload=game;}
    else if(endpoint==='/api/projects' && options.method==='POST'){requests.push({endpoint,body:JSON.parse(options.body)});payload={project:source.projects[1]};}
    else if(endpoint==='/api/setup'){requests.push({endpoint,body:JSON.parse(options.body)});payload={project:source.projects[1],job:{}};}
    return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(payload))};
  };
  w.eval(fs.readFileSync(path.join(base,'static/app.js'),'utf8') + '\nwindow.testBrokerLinkControls = brokerLinkControls;\n;window.__gameChecklist=gameChecklist;');
  await new Promise(resolve=>setTimeout(resolve,30));
  assert.equal(errors.length,0,errors.map(e=>e.stack).join('\n'));
  return dom;
}
async function tick(){await new Promise(resolve=>setTimeout(resolve,15));}
(async()=>{
  const matchingFixture=JSON.parse(JSON.stringify(fixture));
  matchingFixture.romm.matching={mode:'event',modes:{'1':'Every minute','5':'Every 5 minutes','15':'Every 15 minutes','30':'Every 30 minutes','60':'Hourly',event:'Only after an upload',off:'Off (Match now only)'},last_check:null,next_check:null,reason:'Nothing is waiting',pending:[]};
  let matchingDom=await create(null,false,matchingFixture);
  const mw=matchingDom.window,md=mw.document;
  mw.location.hash='settings';await tick();md.getElementById('settings-romm-tab').click();
  const mode=md.getElementById('romm-match-mode');
  assert.equal(mode.options.length,7);assert.equal(mode.value,'event');
  assert(md.getElementById('romm-match-status').textContent.includes('Last check: never'));
  assert(md.getElementById('romm-match-status').textContent.includes('Next check: none'));
  mode.value='5';mode.dispatchEvent(new mw.Event('change',{bubbles:true}));await tick();await tick();
  assert.deepEqual(requests.at(-1),{endpoint:'/api/romm/matching',method:'POST',body:{mode:'5'}});
  assert.equal(mode.value,'5');
  assert(md.getElementById('romm-match-status').textContent.includes('Fixed interval'));
  md.getElementById('romm-match-now').click();await tick();await tick();
  assert.deepEqual(requests.at(-1),{endpoint:'/api/romm/match-now',method:'POST',body:{}});
  assert(md.getElementById('toasts').textContent.includes('RomM checked · 2 updated'));
  assert(md.getElementById('romm-match-status').textContent.includes('Waiting for: Game.zip'));
  matchingFixture.match_error='Import matching failed';
  md.getElementById('romm-match-now').click();await tick();await tick();
  assert(md.getElementById('toasts').textContent.includes('Import matching failed'));
  assert(!md.getElementById('romm-match-now').disabled);
  matchingFixture.romm.has_token=false;matchingFixture.romm.configured=false;mw.pollRefresh();await tick();await tick();
  assert(md.getElementById('romm-match-now').disabled);
  matchingDom.window.close();
  requests.length=0;
  let dom=await create();let w=dom.window,d=w.document;
  w.testBrokerLinkControls(d.body, {}, {});
  const brokerUrl=d.getElementById('broker-url'), brokerCode=d.getElementById('broker-code'), brokerFingerprint=d.getElementById('broker-fingerprint');
  assert(brokerFingerprint, 'Pairing requires the fingerprint from the trusted broker console');
  brokerUrl.value='https://broker:8788';brokerCode.value='1234-5678-9012';brokerFingerprint.value='a'.repeat(64);
  [...d.querySelectorAll('button')].find(button=>button.textContent==='Pair').click();await tick();
  assert.deepEqual(requests.find(request=>request.endpoint==='/api/automation/broker/pair').body,{url:'https://broker:8788',code:'1234-5678-9012',fingerprint:'a'.repeat(64)});
  requests.length=0;

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
  d.getElementById('project-activity-tab').click();await tick();assert.equal(d.getElementById('project-content').getAttribute('aria-labelledby'),'project-activity-tab');
  w.location.hash='settings';await tick();
  const selectLayout=value=>{const radio=d.querySelector(`[name="library-layout"][value="${value}"]`);radio.checked=true;radio.dispatchEvent(new w.Event('change',{bubbles:true}));};
  selectLayout('collection');assert.equal(w.localStorage.getItem('portkeeper.library-layout'),'collection');
  w.location.hash='library';await tick();assert(d.getElementById('project-grid').classList.contains('layout-collection'));assert.equal(d.querySelectorAll('#project-grid .project-card').length,2);
  w.location.hash='settings';await tick();selectLayout('list');w.location.hash='library';await tick();assert(d.getElementById('project-grid').classList.contains('layout-list'));
  const stage=d.getElementById('library-stage');stage.value='attention';stage.dispatchEvent(new w.Event('change',{bubbles:true}));assert.equal(d.querySelectorAll('#project-grid .project-card').length,1);
  dom.window.close();
  dom=await create();w=dom.window;d=w.document;
  assert(d.querySelector('.next-step-card').textContent.includes('Send to RomM'),'A built package that is not in RomM offers to send it');
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();
  assert(d.querySelector('.next-step-card').textContent.includes('Add packaging steps'),'A preserve-only game points to drafting');
  d.getElementById('add-project').click();await tick();await tick();
  assert(d.getElementById('add-game-dialog').open);
  const pick=[...d.querySelectorAll('.game-pick')];assert.equal(pick.length,2,'Catalog games plus a custom game');
  assert(pick[0].textContent.includes('Packaging needs your Banjo-Kazooie'));assert(pick[1].textContent.includes('Custom game'));
  pick[0].click();await tick();[...d.querySelectorAll('#add-game-actions button')].find(n=>n.textContent.includes('Continue')).click();await tick();await tick();
  assert(d.getElementById('add-game-content').textContent.includes('verified as USA 1.0'));
  assert(d.getElementById('add-game-content').textContent.includes('Your ROM is placed where Banjo looks for it.'));
  d.getElementById('add-game-target-windows').click();
  const submit=d.getElementById('add-game-submit');assert.equal(submit.textContent,'Build packages');assert(!submit.disabled);submit.click();await tick();await tick();
  assert.deepEqual(requests[0].body,{definition_id:'banjo-recompiled',targets:['linux'],files:{rom:'n64/roms/Banjo-Kazooie (USA).z64'},keep_updated:false,auto_publish:false,publish:false});
  assert(!d.getElementById('add-game-dialog').open);
  d.getElementById('add-project').click();await tick();await tick();d.querySelector('.game-pick:not(.custom)').click();await tick();[...d.querySelectorAll('#add-game-actions button')].find(n=>n.textContent.includes('Continue')).click();await tick();await tick();
  const destination=d.getElementById('add-game-destination');destination.value='romm';destination.dispatchEvent(new w.Event('change'));
  const updates=d.getElementById('add-game-updates');updates.value='publish';updates.dispatchEvent(new w.Event('change'));
  assert.equal(d.getElementById('add-game-submit').textContent,'Build and send to RomM');d.getElementById('add-game-submit').click();await tick();await tick();
  assert(requests.at(-1).body.publish && requests.at(-1).body.keep_updated && requests.at(-1).body.auto_publish);
  w.location.hash='settings';await tick();d.getElementById('settings-assistant-tab').click();
  assert(d.getElementById('assistant-cloud-fields').hidden);
  const provider=value=>{const radio=d.querySelector(`[name="assistant-provider"][value="${value}"]`);radio.checked=true;radio.dispatchEvent(new w.Event('change',{bubbles:true}));};
  provider('anthropic');assert(!d.getElementById('assistant-cloud-fields').hidden);assert(d.getElementById('assistant-base-url-field').hidden);
  provider('openai');assert(!d.getElementById('assistant-base-url-field').hidden);
  provider('ollama');assert(!d.getElementById('assistant-ollama-fields').hidden);assert(d.getElementById('assistant-cloud-fields').hidden);
  dom.window.close();
  const grouped=JSON.parse(JSON.stringify(fixture));
  grouped.catalog_games=[{id:'bloodborne-linux',game:'Bloodborne',name:'Bloodborne Linux',targets:['linux'],runner:'operations',needs:[],in_library:id(2)},{id:'bloodborne-windows',game:'Bloodborne',name:'Bloodborne Windows',targets:['windows'],runner:'operations',needs:[],in_library:null}];
  dom=await create(null,false,grouped);w=dom.window;d=w.document;d.getElementById('add-project').click();await tick();await tick();
  assert.equal(d.querySelectorAll('.game-pick:not(.custom)').length,1,'One card for platform-specific definitions of the same game');
  d.querySelector('.game-pick:not(.custom)').click();await tick();assert(d.getElementById('add-game-dialog').open,'An existing Linux game does not prevent adding Windows');
  [...d.querySelectorAll('#add-game-actions button')].find(n=>n.textContent.includes('Continue')).click();await tick();await tick();
  assert.equal(d.getElementById('add-game-variant').value,'bloodborne-windows');
  dom.window.close();
  const editable=JSON.parse(JSON.stringify(fixture));
  editable.projects[1].packaging={schema:2,id:'bloodborne',revision:4,name:'Bloodborne',summary:'Original summary',runner:'capture',targets:['linux','windows'],upstream:{repository:'example/game',release_rules:{stable_only:true,tag_pattern:'v.*'}},inputs:[],operations:[],outputs:{},launch:{linux:{command:'game',instructions:'Launch it'}},saves:'saves/',custom_preserved:{nested:true}};
  editable.projects[1].definition_history=[{saved_at:'2026-10-11T12:00:00Z',definition:{...editable.projects[1].packaging,revision:3,summary:'Previous preparation steps'}}];
  dom=await create(null,false,editable);w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();
  d.getElementById('project-advanced-tab').click();await tick();
  assert(!d.getElementById('project-content').textContent.includes('Build progress & logs'));
  const disclosure=label=>[...d.querySelectorAll('#project-content details')].find(node=>node.querySelector('summary')?.textContent===label);
  assert(d.getElementById('project-content').textContent.includes('Previous preparation steps'));
  assert(d.getElementById('project-content').textContent.includes('Remove game from Portkeeper'));
  disclosure('View definition JSON').open=true;disclosure('Previous instructions (1)').open=true;
  editable.catalog={age_seconds:3};editable.projects[1].packaging.summary='Updated by the server';w.pollRefresh();await tick();await tick();
  assert(disclosure('View definition JSON').open,'Definition JSON stays expanded after a polling update');
  assert(disclosure('View definition JSON').textContent.includes('Updated by the server'),'Expanded JSON still shows updated server content');
  assert(disclosure('Previous instructions (1)').open,'Revision history also stays expanded');
  disclosure('View definition JSON').open=false;editable.catalog.age_seconds=6;w.pollRefresh();await tick();await tick();
  assert(!disclosure('View definition JSON').open,'Polling respects an explicitly closed disclosure');
  disclosure('View definition JSON').open=true;
  const stablePreview=disclosure('View definition JSON').querySelector('pre');stablePreview.scrollTop=80;stablePreview.scrollLeft=20;
  const range=d.createRange();range.setStart(stablePreview.firstChild,5);range.setEnd(stablePreview.firstChild,45);w.getSelection().removeAllRanges();w.getSelection().addRange(range);const selected=w.getSelection().toString();
  editable.catalog.age_seconds=7;w.pollRefresh();await tick();await tick();
  assert.equal(disclosure('View definition JSON').querySelector('pre'),stablePreview,'Age-only polls retain the JSON DOM');assert.equal(w.getSelection().toString(),selected);
  editable.jobs=[{id:id(55),project_id:id(2),target:'capture',status:'running',stage:'checking',logs:['New log'],created_at:'2026-10-12T12:00:00Z'}];w.pollRefresh();await tick();await tick();
  const refreshedPreview=disclosure('View definition JSON').querySelector('pre');assert.equal(w.getSelection().toString(),selected,'Actual job updates preserve selected JSON text');assert.equal(refreshedPreview.scrollTop,80);assert.equal(refreshedPreview.scrollLeft,20);
  let copied=null;Object.defineProperty(w.navigator,'clipboard',{configurable:true,value:{writeText:async text=>{copied=text;}}});
  disclosure('View definition JSON').querySelector('button').click();await tick();assert.equal(copied,refreshedPreview.textContent,'Viewer copies the complete displayed JSON');
  Object.defineProperty(w.navigator,'clipboard',{configurable:true,value:undefined});d.execCommand=command=>{assert.equal(command,'copy');copied=d.querySelector('.clipboard-buffer').value;return true;};
  disclosure('View definition JSON').querySelector('button').click();await tick();assert.equal(copied,refreshedPreview.textContent,'Copy fallback works without the secure Clipboard API');assert(!d.querySelector('.clipboard-buffer'));
  editable.jobs=[];w.pollRefresh();await tick();await tick();
  d.getElementById('project-activity-tab').click();await tick();d.getElementById('project-advanced-tab').click();await tick();
  assert(disclosure('View definition JSON').open,'Returning to the same game preserves disclosure choices');
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Ship of Harkinian')).click();await tick();d.getElementById('project-advanced-tab').click();await tick();
  assert(!disclosure('Executable configuration (JSON)').open,'Disclosure choices are not shared with another game');
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();d.getElementById('project-advanced-tab').click();await tick();
  assert(disclosure('View definition JSON').open,'The original game keeps its own choice');
  editable.projects[1].packaging.summary='Original summary';editable.catalog.age_seconds=9;w.pollRefresh();await tick();await tick();
  [...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='Edit packaging definition').click();
  assert(d.getElementById('recipe-dialog').open);assert(!d.getElementById('editor-settings').hidden);
  const summary=d.getElementById('editor-summary');summary.value='My custom build';summary.dispatchEvent(new w.Event('input'));
  d.getElementById('editor-json-tab').click();
  d.getElementById('recipe-copy').click();await tick();assert.equal(copied,d.getElementById('recipe-json').value,'Editor Copy includes the current draft');
  let draft=JSON.parse(d.getElementById('recipe-json').value);assert.equal(draft.summary,'My custom build');assert.deepEqual(draft.custom_preserved,{nested:true});
  d.getElementById('editor-changes-tab').click();assert(d.getElementById('recipe-diff').textContent.includes('Original summary'));assert(d.getElementById('recipe-diff').textContent.includes('My custom build'));
  const before=requests.length;d.getElementById('recipe-validate').click();await tick();
  assert.equal(requests.length,before+1);assert(requests.at(-1).endpoint.endsWith('/configuration/validate'));assert.equal(editable.projects[1].packaging.summary,'Original summary');
  assert(d.getElementById('recipe-validation').textContent.includes('does not run a build'));
  d.getElementById('editor-json-tab').click();d.getElementById('recipe-json').value='{broken';d.getElementById('editor-settings-tab').click();assert(d.getElementById('recipe-form-error').textContent.includes('Fix the JSON'));assert(!d.getElementById('editor-json').hidden);
  d.getElementById('recipe-json').value=JSON.stringify(draft);d.getElementById('recipe-form').dispatchEvent(new w.Event('submit',{cancelable:true,bubbles:true}));await tick();await tick();
  assert(!d.getElementById('recipe-dialog').open);assert.deepEqual(Object.keys(requests.at(-1).body),['packaging']);assert.equal(editable.projects[1].packaging.summary,'My custom build');
  d.getElementById('project-activity-tab').click();await tick();assert(d.getElementById('project-content').textContent.includes('Build progress & logs'));
  dom.window.close();
  const missingPlatform=JSON.parse(JSON.stringify(editable));missingPlatform.projects[1].targets=['windows'];missingPlatform.projects[1].packaging.targets=['windows'];
  dom=await create(null,false,missingPlatform);w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();d.getElementById('project-advanced-tab').click();await tick();
  [...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='Detect supported platforms').click();await tick();await tick();
  assert(d.getElementById('project-platform-'+id(2)+'-linux').checked,'Detection can recover an omitted Linux selection');
  assert(d.getElementById('project-content').textContent.includes('cannot package this OS yet'),'Android detection is informative, not a handheld target');
  [...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='Save platforms').click();await tick();await tick();
  assert.deepEqual(requests.at(-1).body.targets,['linux','windows']);assert(requests.at(-1).body.packaging.targets.includes('linux'));
  d.getElementById('add-project').click();await tick();await tick();d.querySelector('.game-pick.custom').click();await tick();[...d.querySelectorAll('#add-game-actions button')].find(n=>n.textContent.includes('Continue')).click();await tick();
  const repo=d.getElementById('custom-repo');repo.value='example/game';repo.dispatchEvent(new w.Event('input'));repo.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));await tick();await tick();
  assert(d.getElementById('custom-target-linux').checked && d.getElementById('custom-target-windows').checked,'Detected desktop platforms are selected for a custom game');assert(d.getElementById('add-game-content').textContent.includes('Android'));
  dom.window.close();
  const unknownPlatforms=JSON.parse(JSON.stringify(fixture));unknownPlatforms.platform_lookup={repository:'example/game',version:'v1',assets:['release.zip'],platforms:[],platform_detection:{platforms:[],suggested_targets:[]}};
  dom=await create(null,false,unknownPlatforms);w=dom.window;d=w.document;d.getElementById('add-project').click();await tick();await tick();d.querySelector('.game-pick.custom').click();await tick();[...d.querySelectorAll('#add-game-actions button')].find(n=>n.textContent.includes('Continue')).click();await tick();
  const unknownRepo=d.getElementById('custom-repo');unknownRepo.value='example/game';unknownRepo.dispatchEvent(new w.Event('input'));unknownRepo.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));await tick();await tick();
  assert(d.getElementById('custom-add-submit').disabled);assert(!d.getElementById('custom-target-linux').checked && !d.getElementById('custom-target-windows').checked,'Unknown platform support is not guessed');d.getElementById('custom-target-linux').click();assert(!d.getElementById('custom-add-submit').disabled,'Manual platform selection remains usable');
  dom.window.close();
  for(const value of ['collection','list','invalid']){dom=await create(value);assert(dom.window.document.getElementById('project-grid').classList.contains('layout-'+(value==='invalid'?'workspace':value)));dom.window.close();}
  dom=await create(null,true);assert(dom.window.document.getElementById('project-grid').classList.contains('layout-workspace'));dom.window.close();
  const reviewed=JSON.parse(JSON.stringify(editable));
  reviewed.proposals=[{id:id(25),project_id:id(2),kind:'definition',status:'pending_review',source:'Ollama · fixture',definition:reviewed.projects[1].packaging,view:{},
    automatic_checks:{schema:'passed',archive_inspections:{'game-windows.zip':{complete:true}}},draft_job_id:id(26),starter:{definition:{targets:['windows']},unresolved:['Choose launcher']},
    model_review:{status:'needs_attention',summary:'Inspect the settings location.',note:'Model review is advisory. Build and gameplay are untested.',findings:[
      {severity:'issue',field:'saves',message:'Settings use APPDATA.',evidence_status:'quote_found',evidence:{path:'launcher.py',quote:'CONFIG_DIR = APPDATA'}},
      {severity:'uncertain',field:'dependencies',message:'Cannot establish this dependency.',evidence_status:'unconfirmed',evidence:{path:'README.md',quote:'Unmatched quote'}}]}}];
  reviewed.jobs=[{id:id(26),project_id:id(2),status:'ready',target:'capture',kind:'draft',created_at:'2026-10-11T12:00:00Z',finished_at:'2026-10-11T12:01:00Z',logs:[],draft_record_available:true}];
  dom=await create(null,false,reviewed);w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();d.getElementById('project-advanced-tab').click();await tick();
  const reviewText=d.getElementById('project-content').textContent;
  assert(reviewText.includes('Quote found in supplied material'));assert(reviewText.includes('Evidence could not be matched'));assert(reviewText.includes('Uncertain · dependencies'));
  assert(reviewText.includes('Build and gameplay have not been tested'));assert(reviewText.includes('View starter JSON'));
  assert(d.querySelector(`a[href="/api/jobs/${id(26)}/draft-record"]`).hasAttribute('download'));
  reviewed.jobs[0].draft_record_available=false;w.pollRefresh();await tick();await tick();
  assert(!d.querySelector(`a[href="/api/jobs/${id(26)}/draft-record"]`),'Cleared drafting records do not keep a broken download link');
  const reviewDisclosure=[...d.querySelectorAll('details')].find(node=>node.textContent.includes('View review JSON'));reviewDisclosure.open=true;
  let reviewCopied='';w.navigator.clipboard={writeText:async text=>{reviewCopied=text;}};
  [...reviewDisclosure.querySelectorAll('button')].find(node=>node.textContent==='Copy JSON').click();await tick();
  assert.equal(JSON.parse(reviewCopied).status,'needs_attention');dom.window.close();
  const runtimeFiles=JSON.parse(JSON.stringify(editable));
  runtimeFiles.projects[1].packaging.runner='operations';runtimeFiles.projects[1].packaging.inputs=[];
  runtimeFiles.projects[1].packaging.first_run={mode:'asks_user',summary:'Choose your owned game folder in the launcher on your Windows PC.'};
  dom=await create(null,false,runtimeFiles);w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();d.getElementById('project-advanced-tab').click();await tick();
  const gameFiles=[...d.querySelectorAll('.instruction-section')].find(node=>node.querySelector('h3')?.textContent==='3. Your game files');
  assert(gameFiles.textContent.includes('When you play: Choose your owned game folder'),'An empty build-input list still shows runtime game requirements');
  assert(gameFiles.textContent.includes('Runtime-only recipe: no server inputs are configured.'));
  assert(!gameFiles.textContent.includes('No game files are needed.'));dom.window.close();
  const ps4=JSON.parse(JSON.stringify(runtimeFiles));
  const pkgDefinition=JSON.parse(fs.readFileSync(path.join(base,'catalog/definitions/gow3-pc-windows.json'),'utf8'));
  const pkgProject=ps4.projects[1];pkgProject.repo='FelipePrado0/gow3_pc';pkgProject.targets=['windows'];pkgProject.capture_id=id(70);
  ps4.catalog_definition=pkgDefinition;ps4.catalog={definitions:[{id:'gow3_pc',repository:pkgProject.repo,prepares_ps4_pkgs:true}]};
  dom=await create(null,false,ps4);w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();d.getElementById('project-advanced-tab').click();await tick();
  const beforePkgReview=requests.length;
  [...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='Review PKG packaging steps').click();await tick();await tick();
  assert(d.getElementById('recipe-dialog').open);assert(!d.getElementById('editor-changes').hidden);
  assert.deepEqual(JSON.parse(d.getElementById('recipe-json').value).operations[0].packages,['game_pkg','update_pkg']);
  assert.equal(requests.length,beforePkgReview,'Reviewing the preset does not save or build');
  assert.equal(pkgProject.packaging.inputs.length,0,'Existing build steps remain in place until saved');dom.window.close();
  pkgProject.packaging=pkgDefinition;pkgProject.input_bindings={};
  ps4.input_choices={input_root:'/inputs',inputs:[{id:'game_pkg',ps4:{role:'base'},found:[{path:'ps4/roms/random-base.pkg',source:'inputs',ps4:{title_id:'CUSA01623',app_version:'01.00',role:'base'}}],rejected:[{name:'wrong-region.pkg',reason:'expected CUSA01623'}]},{id:'update_pkg',found:[{path:'ps4/roms/random-update.pkg',source:'inputs',ps4:{title_id:'CUSA01623',app_version:'01.02',role:'update'}}],rejected:[]}]};
  dom=await create(null,false,ps4);w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();d.getElementById('project-advanced-tab').click();await tick();
  [...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='Choose a file').click();await tick();await tick();
  const pkgChoice=d.getElementById('input-choice-'+id(2)+'-game_pkg');assert(pkgChoice,'Matching PKG choices appear after searching');assert(pkgChoice.options[1].textContent.includes('CUSA01623 · 01.00 · base'));
  assert(d.getElementById('project-content').textContent.includes('wrong-region.pkg: expected CUSA01623'));
  pkgChoice.selectedIndex=1;pkgChoice.dispatchEvent(new w.Event('change'));
  [...pkgChoice.parentElement.querySelectorAll('button')].find(n=>n.textContent==='Use this file').click();await tick();await tick();
  assert.equal(pkgProject.input_bindings.game_pkg.path,'ps4/roms/random-base.pkg');
  assert(d.getElementById('project-content').textContent.includes('(needed for the selected base)'),'Older bases make the optional update requirement explicit');
  for(const [by,label] of [['md5','verified as USA by MD5 (weaker check)'],['sha1','verified as USA by SHA-1'],['sha256','verified as USA by SHA-256']]){
    Object.assign(pkgProject.input_bindings.game_pkg,{accepted:'USA',verified_by:by});w.pollRefresh();await tick();await tick();
    const shown=d.getElementById('project-content').textContent;assert(shown.includes(label),'Binding shows '+label);assert.equal(shown.includes('weaker check'),by==='md5','Only MD5 is flagged as weaker');
  }
  const preference=d.getElementById('draft-game-files-'+id(2));assert(preference.checked);preference.click();await tick();
  assert(d.querySelector(`a[href="/api/projects/${id(2)}/draft-workspace?prepare_game_files=false"]`));
  [...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='Copy prompt for my own chat').click();await tick();await tick();
  assert.equal(requests.at(-1).body.prepare_game_files,false,'Copied prompts honor the runtime-only choice');dom.window.close();
  const checks=JSON.parse(JSON.stringify(fixture));
  const checkJob=(n,extra={})=>({id:id(n),project_id:id(1),target:'capture',version:'v0.3.8',status:'archived',created_at:'2026-10-13T04:50:00Z',finished_at:'2026-10-13T04:51:00Z',logs:[],...extra});
  checks.jobs=[
    checkJob(80,{upstream_changed:false,check_outcome:'unchanged',message:'No new release or download changes; latest is v0.3.8.'}),
    checkJob(81,{logs:['2026-10-13T04:50:01Z  This source revision and selected assets are already archived']}),
    checkJob(82,{target:'linux',status:'ready',check_outcome:'unchanged',message:'Portable build complete'}),
    checkJob(83,{kind:'draft',check_outcome:'unchanged',logs:['This source revision and selected assets are already archived']}),
    checkJob(84,{kind:'publish',check_outcome:'unchanged',logs:['This source revision and selected assets are already archived']}),
    checkJob(85,{status:'failed',check_outcome:'unchanged',error:'Release lookup failed',logs:['This source revision and selected assets are already archived']}),
    ...['changed','needs_review','builds_queued','packages_current'].map((outcome,n)=>checkJob(86+n,{check_outcome:outcome,message:outcome}))
  ];
  dom=await create(null,false,checks);w=dom.window;d=w.document;w.location.hash='activity';await tick();
  const events=[...d.querySelectorAll('#activity-content .activity-event')];
  const eventWith=text=>events.find(event=>event.querySelector('.field-help').textContent===text);
  const currentCheck=eventWith('No new release or download changes; latest is v0.3.8.');
  assert.equal(currentCheck.querySelector('.status-badge').textContent,'No upstream changes');
  assert(currentCheck.textContent.includes('Check details'));assert(!currentCheck.textContent.includes('Build details'));
  const legacyCheck=eventWith('No new release or download changes were found; latest is v0.3.8.');
  assert.equal(legacyCheck.querySelector('.status-badge').textContent,'No upstream changes','Older unchanged checks use their exact recorded log evidence');
  const ordinaryBuild=eventWith('Portable build complete');
  assert.equal(ordinaryBuild.querySelector('h3').textContent,'Build');assert.equal(ordinaryBuild.querySelector('.status-badge').textContent,'Packaged');
  assert(ordinaryBuild.textContent.includes('Build details'));assert(!ordinaryBuild.textContent.includes('Check details'));
  assert.equal(events.filter(event=>event.querySelector('.status-badge')?.textContent==='No upstream changes').length,2,'Drafts, publication jobs and failures do not inherit update-check outcomes');
  assert.equal(events.filter(event=>event.textContent.includes('Check details')).length,7);
  for(const [outcome,label] of Object.entries({changed:'Release saved',needs_review:'Needs review',builds_queued:'Builds queued',packages_current:'Packages current'}))assert.equal(eventWith(outcome).querySelector('.status-badge').textContent,label);
  assert.equal(eventWith('Release lookup failed').querySelector('.status-badge').textContent,'Needs attention');
  dom.window.close();
  const logViews=JSON.parse(JSON.stringify(fixture));logViews.playtests=[];
  logViews.activity=[{id:id(90),kind:'test',project_id:id(1),target:'linux',result:'starts',notes:'Startup confirmed',created_at:'2026-10-13T05:00:00Z'},{id:id(91),kind:'import',project_id:id(1),target:'linux',message:'Found an existing package',created_at:'2026-10-13T05:00:00Z'}];
  logViews.jobs=[checkJob(92,{target:'linux',status:'ready',artifact_id:id(96)}),checkJob(93,{check_outcome:'unchanged'}),checkJob(94,{kind:'draft',status:'ready'}),checkJob(95,{kind:'publish',target:'linux',status:'ready',artifact_id:id(97)}),checkJob(99,{project_id:id(3),check_outcome:'changed'})];
  logViews.artifacts.push(
    {id:id(96),project_id:id(1),target:'linux',filename:'automatic.zip',created_at:'2026-10-13T05:00:00Z',romm:{status:'awaiting_scan',uploaded_at:'2026-10-13T05:01:00Z'}},
    {id:id(97),project_id:id(1),target:'linux',filename:'manual.zip',created_at:'2026-10-13T05:00:00Z',romm:{status:'in_romm',uploaded_at:'2026-10-13T05:02:00Z'}},
    {id:id(98),project_id:id(1),target:'linux',filename:'cleared-upload.zip',created_at:'2026-10-12T05:00:00Z',romm:{status:'in_romm',uploaded_at:'2026-10-12T05:01:00Z',indexed_at:'2026-10-13T05:05:00Z'}}
  );
  logViews.activity_cutoffs={all:'2026-10-13T00:00:00Z'};
  logViews.notifications=[{id:'unread',message:'New release',kind:'ready',created_at:'2026-10-13T05:00:00Z'}];
  dom=await create(null,false,logViews);w=dom.window;d=w.document;w.location.hash='activity';await tick();
  const activityNodes=()=>[...d.querySelectorAll('#activity-content .activity-event')];
  const logCounts={jobs:8,builds:1,checks:2,drafts:1,uploads:2,tests:1,imports:1,artifacts:4,notifications:1};
  for(const [view,count] of Object.entries(logCounts))assert.equal(d.getElementById(view+'-count').textContent,String(count),'Accurate activity count: '+view);
  assert.equal(d.querySelectorAll('[data-activity-view]').length,10);assert.equal(activityNodes().length,8);
  assert(!d.getElementById('activity-cleanup').hidden);assert.equal(d.getElementById('activity-cleanup').textContent,'Clear all completed activity & logs');
  assert(!activityNodes().some(event=>event.textContent.includes('cleared-upload.zip')),'Indexing later does not resurrect a cleared upload');
  for(const [view,title,count] of [['builds','Build',1],['checks','Update check',2],['drafts','Model draft',1],['uploads','Upload to RomM',2],['tests','Manual test result',1],['imports','Found in RomM',1]]){
    d.getElementById(view+'-tab').click();assert.equal(activityNodes().length,count,'Filter activity by '+view);
    assert(activityNodes().every(event=>event.querySelector('h3').textContent===title));assert.equal(d.getElementById('activity-content').getAttribute('aria-labelledby'),view+'-tab');
    assert(d.getElementById('activity-type-field').hidden);assert.equal(d.getElementById(view+'-tab').tabIndex,0);
    assert(d.getElementById('activity-cleanup').hidden,'Category views never offer global cleanup');
  }
  d.getElementById('uploads-tab').click();
  assert.equal(activityNodes().filter(event=>event.textContent.includes('automatic.zip')).length,1,'Automatic uploads inside build jobs appear in Uploads');
  assert.equal(activityNodes().filter(event=>event.textContent.includes('Uploaded · waiting for RomM')).length,1);
  assert.equal(activityNodes().length,2,'Explicit publication jobs are not duplicated by artifact upload metadata');
  d.getElementById('jobs-tab').click();const show=d.getElementById('activity-type-filter');show.value='uploads';show.dispatchEvent(new w.Event('change'));assert.equal(activityNodes().length,2);
  d.getElementById('builds-tab').click();assert.equal(activityNodes().length,1,'Dedicated tabs are independent of the All activity Show filter');
  const gameFilter=d.getElementById('activity-game-filter');gameFilter.value=id(2);gameFilter.dispatchEvent(new w.Event('change'));
  d.getElementById('checks-tab').click();assert.equal(activityNodes().length,1);assert(activityNodes()[0].textContent.includes('Bloodborne Windows'),'A game filter includes related platform projects');
  assert.equal(d.getElementById('jobs-count').textContent,'1');assert.equal(d.getElementById('checks-count').textContent,'1');assert.equal(d.getElementById('uploads-count').textContent,'0');
  d.getElementById('jobs-tab').click();assert(d.getElementById('activity-cleanup').hidden,'Selecting a game hides global cleanup');
  d.getElementById('packages-tab').click();assert(d.getElementById('activity-content').textContent.includes('No build archives'));
  gameFilter.value='';gameFilter.dispatchEvent(new w.Event('change'));assert.equal(d.getElementById('artifacts-count').textContent,'4');assert.equal(d.querySelectorAll('#activity-content tbody tr').length,4,'Cleanup hides activity while retaining build archives');
  const tabs=d.querySelector('.activity-tabs');d.getElementById('builds-tab').focus();tabs.dispatchEvent(new w.KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true}));
  assert.equal(d.activeElement.id,'checks-tab');assert.equal(d.getElementById('checks-tab').getAttribute('aria-selected'),'true');assert.equal(activityNodes().length,2);
  tabs.dispatchEvent(new w.KeyboardEvent('keydown',{key:'End',bubbles:true,cancelable:true}));assert.equal(d.activeElement.id,'notifications-tab');assert.equal(d.getElementById('activity-content').getAttribute('aria-labelledby'),'notifications-tab');
  tabs.dispatchEvent(new w.KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true}));assert.equal(d.activeElement.id,'jobs-tab');
  tabs.dispatchEvent(new w.KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true,cancelable:true}));assert.equal(d.activeElement.id,'notifications-tab');
  tabs.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Home',bubbles:true,cancelable:true}));assert.equal(d.activeElement.id,'jobs-tab');
  assert.equal(d.querySelectorAll('.activity-tabs [tabindex="0"]').length,1,'Activity tabs use one keyboard entry point');
  assert(stylesheet.includes('#activity-page>.activity-tabs{flex-wrap:nowrap') && stylesheet.includes('overflow-x:auto'),'Mobile activity tabs scroll without wrapping');
  logViews.artifacts.find(artifact=>artifact.id===id(96)).romm={status:'failed',error:'The server rejected the automatic upload'};w.pollRefresh();await tick();await tick();d.getElementById('uploads-tab').click();
  const failedUpload=activityNodes().find(event=>event.textContent.includes('The server rejected the automatic upload'));
  assert(failedUpload,'Failed automatic uploads use the associated build job timestamp if the publication record has none');assert.equal(failedUpload.querySelector('.status-badge').textContent,'Needs attention');
  dom.window.close();
  const subscriptions=JSON.parse(JSON.stringify(fixture));
  subscriptions.assistant={provider:'ollama',configured:true,drafting_model:'local-model',agents:{codex:{installed:true,authenticated:false,billing:'subscription'},claude_code:{installed:true,authenticated:true,billing:'subscription'},gemini:{installed:false,authenticated:false,message:'Gemini CLI is not installed. Use the server terminal to install it.'}},agent_models:{codex:'saved-codex',claude_code:'saved-claude'}};
  subscriptions.agent_model_choices={codex:['saved-codex','another-codex'],claude_code:['saved-claude'],gemini:['free-gemini']};
  dom=await create(null,false,subscriptions);w=dom.window;d=w.document;w.location.hash='settings';await tick();d.getElementById('settings-assistant-tab').click();
  const chooseProvider=value=>{const node=d.querySelector(`[name="assistant-provider"][value="${value}"]`);node.checked=true;node.dispatchEvent(new w.Event('change',{bubbles:true}));};
  chooseProvider('codex');assert(!d.getElementById('assistant-agent-fields').hidden);assert(d.getElementById('assistant-cloud-fields').hidden);assert(d.getElementById('assistant-ollama-fields').hidden);
  assert.equal(d.querySelectorAll('#assistant-agent-fields input[type="password"]').length,0,'Native sign-in never asks for an account password');
  assert(d.getElementById('assistant-agent-allowance').textContent.includes('never switches to a paid API'));assert(d.getElementById('assistant-agent-allowance').textContent.includes('disable extra usage or credit spending'));assert(d.getElementById('assistant-test').textContent.includes('uses allowance'));assert(d.getElementById('assistant-test').disabled);
  assert.equal(d.getElementById('assistant-agent-model').value,'saved-codex');
  const beforeModels=requests.length;d.getElementById('assistant-agent-models-refresh').click();await tick();
  assert.deepEqual(requests.at(-1),{endpoint:'/api/assistant/models',body:{provider:'codex'}});assert.equal(requests.length,beforeModels+1,'Loading model choices makes no inference request');
  assert(d.getElementById('assistant-agent-models-status').textContent.includes('No model request'));assert.equal(d.getElementById('assistant-agent-model').value,'saved-codex');
  const subscriptionModel=d.getElementById('assistant-agent-model');subscriptionModel.value='another-codex';subscriptionModel.dispatchEvent(new w.Event('change'));chooseProvider('claude_code');assert.equal(subscriptionModel.value,'saved-claude');assert(d.getElementById('assistant-agent-account').textContent.includes('Signed in'));
  chooseProvider('codex');assert.equal(subscriptionModel.value,'another-codex','Unsaved per-provider model choices survive switching providers');
  const beforeSignIn=requests.length;d.getElementById('assistant-agent-login').click();await tick();
  assert.deepEqual(requests.at(-1),{endpoint:'/api/assistant/agent/login',body:{provider:'codex'}});assert.equal(requests.length,beforeSignIn+1,'Starting native sign-in never tests a model or refreshes provider state');
  assert.equal(d.getElementById('assistant-agent-login-link').querySelector('a').href,'https://auth.example.test/device');assert.equal(d.getElementById('assistant-agent-login-code').textContent,'ONE-TIME-CODE');
  assert(d.getElementById('assistant-agent-login-input-row').hidden);d.getElementById('assistant-agent-login-input').value='not-authorized';d.getElementById('assistant-agent-login-submit').click();await tick();assert.equal(requests.length,beforeSignIn+1,'Callback submission is rejected unless the native flow explicitly needs input');
  subscriptions.agent_logins.codex={provider:'codex',status:'connected',message:'Native sign-in complete'};d.getElementById('assistant-agent-login-check').click();await tick();await tick();
  assert(d.getElementById('assistant-agent-account').textContent.includes('Signed in'));assert(d.getElementById('assistant-agent-login-code-row').hidden);assert(d.getElementById('assistant-agent-login-input-row').hidden);assert.equal(d.getElementById('assistant-agent-login-input').value,'');
  d.getElementById('assistant-api-key').value='API-KEY-MUST-NOT-BE-SENT';d.getElementById('assistant-form').dispatchEvent(new w.Event('submit',{cancelable:true,bubbles:true}));await tick();await tick();
  assert.deepEqual(requests.at(-1),{endpoint:'/api/assistant/provider',body:{provider:'codex',model:'another-codex'}});assert.equal(subscriptions.assistant.agent_models.claude_code,'saved-claude','Saving one provider preserves another provider’s settings');assert(!d.getElementById('assistant-test').disabled);
  const beforeTest=requests.length;d.getElementById('assistant-test').click();await tick();assert.equal(requests.length,beforeTest+1);assert.equal(requests.at(-1).endpoint,'/api/assistant/test','Only an explicit test sends the allowance-consuming connection request');
  d.getElementById('assistant-agent-logout').click();await tick();await tick();assert.equal(requests.at(-1).endpoint,'/api/assistant/agent/logout');assert(d.getElementById('assistant-agent-account').textContent.includes('Sign-in needed'));assert(d.getElementById('assistant-test').disabled);
  chooseProvider('gemini');assert(d.getElementById('assistant-agent-login').disabled);assert(d.getElementById('assistant-agent-account').textContent.includes('server terminal'));assert(d.getElementById('assistant-agent-allowance').textContent.includes('free tier is capped'));
  subscriptions.assistant.agents.gemini.installed=true;subscriptions.agent_login_start={gemini:{provider:'gemini',status:'waiting',message:'Paste the native authorization response',url:'javascript:alert(1)',needs_input:true}};w.pollRefresh();await tick();await tick();chooseProvider('gemini');d.getElementById('assistant-agent-login').click();await tick();
  assert(!d.getElementById('assistant-agent-login-link').querySelector('a'),'Unsafe sign-in URLs are never linked');assert(!d.getElementById('assistant-agent-login-input-row').hidden);d.getElementById('assistant-agent-login-input').value='ONE-TIME-AUTHORIZATION-RESPONSE';d.getElementById('assistant-agent-login-submit').click();await tick();await tick();
  assert.deepEqual(requests.at(-1),{endpoint:'/api/assistant/agent/login/input',body:{provider:'gemini',value:'ONE-TIME-AUTHORIZATION-RESPONSE'}});assert(d.getElementById('assistant-agent-login-input-row').hidden);assert.equal(d.getElementById('assistant-agent-login-input').value,'');
  const nativeCommand='setpriv --reuid 99 --regid 100 --clear-groups env -i HOME=/data/private/codex codex login --device-auth';
  chooseProvider('codex');subscriptions.agent_login_start.codex={provider:'codex',status:'failed',message:'This native sign-in requires the server terminal.',terminal_command:nativeCommand,raw_output:'PRIVATE-NATIVE-SESSION-OUTPUT'};d.getElementById('assistant-agent-login').click();await tick();assert(d.getElementById('assistant-agent-login-message').textContent.includes('requires the server terminal'));assert(!d.getElementById('assistant-agent-login').disabled);
  const terminalHelp=d.getElementById('assistant-agent-terminal-help');assert(!terminalHelp.hidden);assert(!terminalHelp.open,'Terminal sign-in help starts collapsed');assert.equal(d.getElementById('assistant-agent-terminal-command').textContent,nativeCommand);assert(terminalHelp.textContent.includes('Portkeeper container’s Console'));assert(!d.getElementById('settings-assistant').textContent.includes('PRIVATE-NATIVE-SESSION-OUTPUT'),'Native raw session output is never rendered');
  let copiedNativeCommand='';Object.defineProperty(w.navigator,'clipboard',{configurable:true,value:{writeText:async text=>{copiedNativeCommand=text;}}});const beforeCommandCopy=requests.length;d.getElementById('assistant-agent-terminal-copy').click();await tick();assert.equal(copiedNativeCommand,nativeCommand);assert.equal(requests.length,beforeCommandCopy,'Copying a terminal command never executes it or calls the server');
  assert(!requests.slice(beforeSignIn).some(request=>request.endpoint==='/api/assistant/provider' && request.body.api_key),'Native provider settings contain no API credentials');
  subscriptions.assistant.provider='claude_code';subscriptions.assistant.configured=true;subscriptions.assistant.agents.claude_code.billing='api_key';w.pollRefresh();await tick();await tick();chooseProvider('claude_code');
  assert(d.getElementById('assistant-agent-account').textContent.includes('This CLI is using API billing'));assert(d.getElementById('assistant-agent-account').textContent.includes('Sign-in needed'));assert(d.getElementById('assistant-test').disabled,'API-key billing is never treated as usable subscription authentication');
  subscriptions.assistant.agents.gemini.authenticated=false;w.pollRefresh();await tick();await tick();chooseProvider('gemini');assert(d.getElementById('assistant-agent-account').textContent.includes('Sign-in needed'),'An expired account overrides a previously completed browser login');
  assert(!d.getElementById('settings-assistant').textContent.includes('nothing a model drafts runs until'));assert(d.getElementById('settings-assistant').textContent.includes('Optional test builds run in a temporary workspace'));dom.window.close();
  const resumed=JSON.parse(JSON.stringify(fixture));resumed.assistant={provider:'codex',configured:false,agents:{codex:{installed:true,authenticated:false,billing:'subscription'}}};
  resumed.agent_logins={codex:{provider:'codex',status:'waiting',message:'Native device sign-in is still pending',url:'https://auth.example.test/resume',code:'RESUMED-ONE-TIME-CODE'}};
  const beforeResume=requests.length;dom=await create(null,false,resumed);w=dom.window;d=w.document;w.location.hash='settings';await tick();d.getElementById('settings-assistant-tab').click();await tick();await tick();
  assert.equal(requests.length,beforeResume+1);assert.equal(requests.at(-1).endpoint,'/api/assistant/agent/login?provider=codex','Opening the saved native provider resumes its pending session through the public status endpoint');
  assert.equal(d.getElementById('assistant-agent-login-code').textContent,'RESUMED-ONE-TIME-CODE');assert.equal(d.getElementById('assistant-agent-login-link').querySelector('a').href,'https://auth.example.test/resume');assert(d.getElementById('assistant-agent-terminal-help').hidden,'No console fallback is shown unless the native adapter supplies one');
  const pickAfterRefresh=value=>{const node=d.querySelector(`[name="assistant-provider"][value="${value}"]`);node.checked=true;node.dispatchEvent(new w.Event('change',{bubbles:true}));};pickAfterRefresh('ollama');pickAfterRefresh('codex');await tick();assert.equal(requests.length,beforeResume+1,'A recovered session is not repeatedly checked just by switching provider fields');
  const storedPreferences=JSON.stringify(Array.from({length:w.localStorage.length},(_,n)=>{const key=w.localStorage.key(n);return [key,w.localStorage.getItem(key)];}));assert(!storedPreferences.includes('RESUMED-ONE-TIME-CODE') && !storedPreferences.includes('https://auth.example.test/resume'),'Native URLs and one-time codes are never saved in browser storage');
  resumed.agent_logins.codex={provider:'codex',status:'connected',message:'Native sign-in complete',terminal_command:nativeCommand};d.getElementById('assistant-agent-login-check').click();await tick();await tick();assert(d.getElementById('assistant-agent-terminal-help').hidden);assert.equal(d.getElementById('assistant-agent-terminal-command').textContent,'');assert.equal(d.getElementById('assistant-agent-login-code').textContent,'','One-time sign-in details are cleared after connection');dom.window.close();
  const investigated=JSON.parse(JSON.stringify(editable));investigated.projects[1].capture_id=id(88);investigated.assistant={provider:'ollama',configured:true,drafting_model:'Local fixture'};
  const investigationReport={id:id(90),kind:'investigation',project_id:id(2),status:'pending_review',prepare_game_files:true,source:'Local fixture',plan:{outcome:'unsupported',summary:'This release needs a source-build adapter.',targets:[{target:'windows',method:'source_build'}],game_files:{needed_to_play:true,needed_for_packaging:true,requirements:'Your owned game data'},steps:[{capability:'source_build',purpose:'Compile the game'}],evidence:[{source:'e2',path:'README.md',quote:'Build from source',supports:'Compilation',matched:true}],uncertainties:[],capability_problems:['Source compilation needs a reviewed adapter'],can_draft:false}};
  investigated.proposals=[investigationReport];
  dom=await create(null,false,investigated);w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();d.getElementById('project-advanced-tab').click();await tick();
  assert(d.querySelector('.investigation-card').textContent.includes('Current runner cannot package this release'));assert(![...d.querySelectorAll('button')].some(n=>n.textContent==='Draft packaging steps from this plan'),'Unsupported reports cannot be drafted');
  const investigationLimit=d.getElementById('investigation-limit-'+id(2));investigationLimit.value='6';investigationLimit.dispatchEvent(new w.Event('change'));
  [...d.querySelectorAll('.investigation-card button')].find(n=>n.textContent==='Investigate again').click();await tick();await tick();assert.equal(requests.at(-1).body.max_calls,6);
  investigationReport.plan={...investigationReport.plan,outcome:'ready',summary:'A supported runtime is available.',targets:[{target:'windows',method:'release',asset:'game-windows.zip',launch:'Game.exe'}],steps:[{capability:'unpack_download',purpose:'Extract the playable runtime'}],capability_problems:[],can_draft:true};w.pollRefresh();await tick();await tick();
  const consent=d.getElementById('investigation-inputs-'+id(2));assert(!consent.checked,'Private-input tests are opt-in');
  [...d.querySelectorAll('.investigation-card button')].find(n=>n.textContent==='Draft packaging steps from this plan').click();await tick();await tick();assert.equal(requests.at(-1).body.investigation_id,id(90));assert.equal(requests.at(-1).body.test_inputs,false);
  d.getElementById('investigation-inputs-'+id(2)).click();[...d.querySelectorAll('.investigation-card button')].find(n=>n.textContent==='Draft packaging steps from this plan').click();await tick();await tick();assert.equal(requests.at(-1).body.test_inputs,true);dom.window.close();
  const failedDraft=JSON.parse(JSON.stringify(editable));failedDraft.projects[1].capture_id=id(88);
  failedDraft.assistant={provider:'claude_code',drafting_model:'Claude default',configured:true};
  failedDraft.jobs=[{id:id(89),project_id:id(2),kind:'draft',status:'failed',target:'capture',error:'Operation 2 path must start with $package or $shared',draft_record_available:true,draft_max_calls:1,logs:[],created_at:'2026-10-13T14:00:00Z',finished_at:'2026-10-13T14:01:00Z'}];
  const rawResponse=JSON.stringify({explanation:'A raw model response',definition:{targets:['windows'],operations:[{op:'write_text',path:'README.txt',text:'<script>window.untrustedExecuted=true</script>'}]}},null,2);
  failedDraft.draft_record={attempts:[{kind:'draft',attempt:1,result:'rejected',response:'{"targets":["ps4"]}',error:'Unsupported target ps4'},{kind:'draft',attempt:2,result:'rejected',response:rawResponse,error:failedDraft.jobs[0].error}]};
  dom=await create(null,false,failedDraft);w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();d.getElementById('project-advanced-tab').click();await tick();
  assert(!d.getElementById('draft-corrections-'+id(2)).checked,'Extra model calls are opt-in');
  [...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='View model responses').click();await tick();await tick();
  const responseDetails=[...d.querySelectorAll('.model-responses details')];assert.equal(responseDetails.length,2);
  assert(responseDetails[0].textContent.includes('Unsupported target ps4'));assert(responseDetails[1].open);
  const rawPreview=responseDetails[1].querySelector('pre');assert.equal(rawPreview.textContent,rawResponse);assert(!w.untrustedExecuted);
  let rawCopied='';w.navigator.clipboard={writeText:async text=>rawCopied=text};responseDetails[1].querySelector('button').click();await tick();assert.equal(rawCopied,rawResponse);
  rawPreview.scrollTop=42;const selection=d.createRange();selection.setStart(rawPreview.firstChild,5);selection.setEnd(rawPreview.firstChild,25);w.getSelection().removeAllRanges();w.getSelection().addRange(selection);const selectedRaw=w.getSelection().toString();
  failedDraft.jobs[0].logs.push('New log');w.pollRefresh();await tick();await tick();assert.equal(w.getSelection().toString(),selectedRaw);assert.equal([...d.querySelectorAll('.model-responses pre')][1].scrollTop,42);
  const retryButton=()=>[...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='Retry with corrections');
  const retryCard=retryButton().closest('.draft-card');assert(retryCard.textContent.includes('up to 3 model requests'),'Retry states its cost');
  const failureRow=[...retryCard.children];assert(failureRow.findIndex(n=>n.contains(retryButton()))<failureRow.findIndex(n=>n.classList.contains('model-responses')),'Retry sits above the model responses');
  retryButton().click();await tick();await tick();assert.equal(requests.at(-1).body.max_calls,3,'Retry sends the corrections limit');
  assert(!d.getElementById('draft-corrections-'+id(2)).checked,'A one-off retry does not change the saved default');
  failedDraft.jobs[0].status='ready';failedDraft.jobs[0].error=null;w.pollRefresh();await tick();await tick();assert(!retryButton(),'A successful retry clears the failed state');
  failedDraft.jobs[0].status='failed';failedDraft.jobs[0].error='Operation 2 path must start with $package or $shared';w.pollRefresh();await tick();await tick();
  [...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='Draft with Claude default').click();await tick();await tick();assert.equal(requests.at(-1).body.max_calls,1);
  d.getElementById('draft-corrections-'+id(2)).click();
  [...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='Draft with Claude default').click();await tick();await tick();assert.equal(requests.at(-1).body.max_calls,3);
  d.getElementById('project-activity-tab').click();await tick();assert(d.querySelector('.job-panel .model-responses'),'Model responses are accessible beside the job logs too');dom.window.close();
  const failedInvestigation=JSON.parse(JSON.stringify(failedDraft));failedInvestigation.draft_record=null;
  failedInvestigation.jobs=[{id:id(95),project_id:id(2),kind:'investigation',status:'failed',error:'evidence[0].quote was not found in README.md',investigation_max_calls:1,logs:[],created_at:'2026-10-13T14:00:00Z',finished_at:'2026-10-13T14:01:00Z'}];
  dom=await create(null,false,failedInvestigation);w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();d.getElementById('project-advanced-tab').click();await tick();
  const investigationRetry=()=>[...d.querySelectorAll('.investigation-card button')].find(n=>n.textContent==='Retry with corrections');
  investigationRetry().click();await tick();await tick();assert(requests.at(-1).endpoint.endsWith('/investigate'));assert.equal(requests.at(-1).body.max_calls,3,'Investigation retry raises the limit to the corrections minimum');
  const retryLimit=d.getElementById('investigation-limit-'+id(2));retryLimit.value='6';retryLimit.dispatchEvent(new w.Event('change'));
  investigationRetry().click();await tick();await tick();assert.equal(requests.at(-1).body.max_calls,6,'Investigation retry keeps a higher selected limit');dom.window.close();
  failedInvestigation.jobs[0].investigation_max_calls=6;dom=await create(null,false,failedInvestigation);w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();d.getElementById('project-advanced-tab').click();await tick();
  [...d.querySelectorAll('.investigation-card button')].find(n=>n.textContent==='Retry with corrections').click();await tick();await tick();assert.equal(requests.at(-1).body.max_calls,6,'Investigation retry keeps the saved job limit after a reload');dom.window.close();
  const automatic=JSON.parse(JSON.stringify(fixture));
  automatic.projects=[{id:id(22),name:'Unknown engine',repo:'example/unknown',targets:['linux'],recipe:{mode:'capture'},capture_id:id(77),status:'archived',metadata:{}}];automatic.artifacts=[];automatic.romm={};
  automatic.assistant={provider:'openai',configured:true,drafting_model:'Fixture model'};
  automatic.automation={policy:{enabled:true,privacy:'strict',eligible_providers:['openai'],max_calls:20},worker:{ready:true,backend:'fixture',limits:{workspace_bytes:2147483648}}};
  dom=await create(null,false,automatic);w=dom.window;d=w.document;
  automatic.automation.worker={ready:false,offline:true};w.pollRefresh();await tick();await tick();
  const offlineButton=[...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='Set up automatically');
  assert(offlineButton && !offlineButton.disabled,'A paired offline broker still permits queuing automatic setup');
  d.getElementById('project-advanced-tab').click();await tick();
  const offlineSubmit=[...d.querySelectorAll('.automatic-setup button')].find(n=>n.textContent==='Set up automatically');
  assert(offlineSubmit && !offlineSubmit.disabled,'Advanced setup also permits offline submission');
  offlineSubmit.click();await tick();await tick();assert(requests.at(-1).endpoint.endsWith('/automate'));
  automatic.automation.worker={ready:true};w.pollRefresh();await tick();await tick();
  d.getElementById('project-overview-tab').click();await tick();
  const autoButton=[...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='Set up automatically');assert(autoButton,'Automatic setup is offered when the broker/provider are enabled');
  autoButton.click();await tick();await tick();assert(requests.at(-1).endpoint.endsWith('/automate'));assert.equal(requests.at(-1).body.prepare_game_files,true);assert.equal(requests.at(-1).body.publish,false);
  d.getElementById('project-advanced-tab').click();await tick();
  [...d.querySelectorAll('.automatic-setup button')].find(n=>n.textContent==='Browse my game collection').click();await tick();await tick();
  [...d.querySelectorAll('.automatic-setup button')].find(n=>n.textContent==='Use this file').click();await tick();
  assert(JSON.parse(d.getElementById('automatic-inputs-'+id(22)).value).some(i=>i.path==='owned.bin'),'File browsing fills the editable selection without requiring typed JSON');
  const scripts=[{script:'mkdir -p out/linux',phase:'public',target:'linux',timeout:600}];
  const autoManifest={summary:'Generated fixture',game_files:{required:true,included:true,instructions:'Your owned data is included.'},packages:{linux:{command:'game',instructions:'Run game'}},saves:'Unknown',limitations:['Gameplay untested']};
  automatic.projects[0].autonomous_recipe={revision:1,targets:['linux'],steps:scripts,manifest:autoManifest};
  w.pollRefresh();await tick();await tick();
  const scriptEditor=d.getElementById('script-recipe-editor-'+id(22));assert(scriptEditor);
  scriptEditor.value=JSON.stringify({steps:scripts,manifest:{...autoManifest,summary:'My edited recipe'}},null,2);scriptEditor.dispatchEvent(new w.Event('input'));scriptEditor.focus();scriptEditor.setSelectionRange(5,20);
  automatic.jobs=[{id:id(23),project_id:id(22),kind:'automation',target:'capture',status:'running',stage:'building',logs:['Working'],created_at:'2026-10-13T00:00:00Z'}];
  w.pollRefresh();await tick();await tick();
  assert.equal(d.activeElement.id,'script-recipe-editor-'+id(22));assert.equal(d.activeElement.selectionStart,5);assert.equal(d.activeElement.selectionEnd,20);assert(d.activeElement.value.includes('My edited recipe'));
  automatic.jobs[0].status='ready';automatic.jobs[0].stage='complete';w.pollRefresh();await tick();await tick();
  [...d.querySelectorAll('.automatic-setup button')].find(n=>n.textContent==='Save script changes').click();await tick();await tick();assert(requests.at(-1).endpoint.endsWith('/script-recipe'));assert.equal(requests.at(-1).body.manifest.summary,'My edited recipe');
  automatic.jobs[0]={...automatic.jobs[0],status:'waiting_user',stage:'waiting_user',user_request:{id:'question-one',kind:'input',role:'base_game',input_kind:'file',question:'Choose your Minish Cap ROM',candidates:[{role:'base_game',kind:'file',path:'gba/Minish Cap.gba'},{role:'base_game',kind:'file',path:'gba/Other Game.gba'}]}};
  w.pollRefresh();await tick();await tick();
  assert(d.querySelector('.automatic-question').textContent.includes('Choose your Minish Cap ROM'));
  d.getElementById('project-overview-tab').click();await tick();
  assert(d.querySelector('.automatic-question'),'The file choice is directly on Play, without switching tabs');
  assert.equal(d.querySelectorAll('.automatic-question').length,1,'There is only one place to answer');
  let picker=d.getElementById('automatic-answer-input-'+id(23)+'-0');assert(picker);assert.equal(picker.value,'gba/Minish Cap.gba');
  picker.checked=true;picker.dispatchEvent(new w.Event('change'));await tick();
  assert(w.location.hash.endsWith('/overview'),'Choosing a detected file stays on the same tab');
  const note=[...d.querySelectorAll('.automatic-question summary')].find(n=>n.textContent==='Add a note (optional)');note.parentElement.open=true;
  let answer=d.getElementById('automatic-answer-'+id(23));answer.value='Use the US version';answer.dispatchEvent(new w.Event('input'));answer.focus();answer.setSelectionRange(4,10);
  automatic.jobs[0].logs=['Waiting for your selection'];w.pollRefresh();await tick();await tick();
  assert.equal(d.activeElement.id,'automatic-answer-'+id(23));assert.equal(d.activeElement.selectionStart,4);assert.equal(d.activeElement.selectionEnd,10);assert.equal(d.activeElement.value,'Use the US version');
  [...d.querySelectorAll('.automatic-question button')].find(n=>n.textContent==='Continue setup').click();await tick();await tick();
  assert(requests.at(-1).endpoint.endsWith('/reply-automation'));assert.deepEqual(requests.at(-1).body,{request_id:'question-one',reply:'Use the US version',input:{role:'base_game',kind:'file',path:'gba/Minish Cap.gba'}});
  automatic.jobs[0].user_request={id:'browse-question',kind:'input',role:'base_game',input_kind:'file',question:'Choose another game file',candidates:[]};
  w.pollRefresh();await tick();await tick();d.getElementById('project-activity-tab').click();await tick();
  const browseSection=[...d.querySelectorAll('.automatic-question details')].find(n=>n.querySelector('summary')?.textContent==='Choose another file or folder');
  assert(!browseSection.open,'The collection browser starts collapsed');browseSection.open=true;await tick();await tick();
  assert(w.location.hash.endsWith('/activity'),'Opening the fallback browser stays beside the run');
  [...d.querySelectorAll('.automatic-browser-item')].find(n=>n.textContent==='owned.bin').click();await tick();
  assert(w.location.hash.endsWith('/activity'),'Selecting a browsed file does not switch tabs');
  assert(d.querySelector('.automatic-match').textContent.includes('Selected manually'));
  [...d.querySelectorAll('.automatic-question button')].find(n=>n.textContent==='Continue setup').click();await tick();await tick();
  assert.equal(requests.at(-1).body.input.path,'owned.bin');
  automatic.jobs[0].user_request={id:'filter-question',kind:'input',role:'base_game',input_kind:'file',question:'Choose a detected game file',candidates:[{role:'base_game',kind:'file',path:'gba/Minish Cap.gba'},...Array.from({length:8},(_,i)=>({role:'base_game',kind:'file',path:`gba/Other ${i}.gba`}))]};
  w.pollRefresh();await tick();await tick();
  const filter=d.getElementById('automatic-match-search-'+id(23));filter.value='minish';filter.dispatchEvent(new w.Event('input'));
  assert.equal([...d.querySelectorAll('.automatic-match')].filter(n=>!n.hidden).length,1,'Detected matches can be filtered without browsing the collection');
  automatic.jobs[0].logs=['Still waiting'];w.pollRefresh();await tick();await tick();
  assert.equal(d.getElementById('automatic-match-search-'+id(23)).value,'minish');
  assert.equal([...d.querySelectorAll('.automatic-match')].filter(n=>!n.hidden).length,1,'Polling preserves the file filter');
  automatic.jobs[0].user_request={id:'question-two',kind:'question',question:'Which build variant?',choices:['Standard','Experimental']};
  w.pollRefresh();await tick();await tick();
  assert([...d.querySelectorAll('.automatic-question button')].find(n=>n.textContent==='Continue setup').disabled);
  [...d.querySelectorAll('.automatic-question button')].find(n=>n.textContent==='Standard').click();await tick();
  [...d.querySelectorAll('.automatic-question button')].find(n=>n.textContent==='Continue setup').click();await tick();await tick();
  assert.deepEqual(requests.at(-1).body,{request_id:'question-two',reply:'Standard'});
  d.getElementById('project-activity-tab').click();await tick();assert(d.querySelector('.automatic-question'),'Questions are answerable beside the progress logs');
  dom.window.close();
  const discoveryFixture=JSON.parse(JSON.stringify(fixture));
  discoveryFixture.catalog_games=[{id:'banjo-recompiled',name:'Banjo Recompiled',repository:'BanjoRecomp/BanjoRecomp',targets:['linux','windows'],runner:'operations',needs:[{label:'Banjo-Kazooie (USA) 1.0 N64 ROM'}],first_run:{mode:'preloaded'},in_library:null}];
  discoveryFixture.discovery={source:'Recomp Launcher',count:2,updated:'2026-11-03',fetched_at:'2026-11-03T00:00:00Z',stale:false,refreshing:false};
  discoveryFixture.discovery_games=[
    {id:'banjo',name:'Banjo Recompiled',game:'Banjo-Kazooie',origin:'n64',repository:'BanjoRecomp/BanjoRecomp',systems:['windows','linux'],has_cover:true,in_library:null,known:true},
    {id:'new-port',name:'New port <img src=x onerror=alert(1)>',game:'New game',origin:'ps2',repository:null,systems:['windows'],has_cover:false,in_library:null,known:false}
  ];
  discoveryFixture.platform_lookup={repository:'example/new-port',version:'v1',assets:['port-windows.zip'],platforms:['windows'],platform_detection:{suggested_targets:['windows'],platforms:[]}};
  dom=await create(null,false,discoveryFixture);w=dom.window;d=w.document;
  const discoveryRequestCount=requests.filter(r=>r.endpoint.startsWith('/api/discovery')).length;
  d.getElementById('add-project').click();await tick();await tick();
  assert.equal(requests.filter(r=>r.endpoint.startsWith('/api/discovery')).length,discoveryRequestCount,'Opening the normal game picker does not contact the reference site');
  [...d.querySelectorAll('#add-game-content button')].find(n=>n.textContent==='Browse all Recomp games').click();await tick();await tick();
  assert.equal(d.querySelectorAll('.discovery-result').length,2);
  assert(!d.querySelector('.discovery-result img[src*="onerror"]')&&!d.querySelector('.discovery-result strong img'),'Reference names render as plain text');
  assert(d.querySelector('.discovery-cover').getAttribute('src')==='/api/discovery/image/banjo/cover','Artwork is requested through Portkeeper, never hotlinked');
  assert(d.getElementById('game-discovery').textContent.includes('Recomp catalog: 2 games, updated 2026-11-03'));
  d.getElementById('discovery-origin').value='ps2';d.getElementById('discovery-origin').dispatchEvent(new w.Event('change'));await tick();await tick();
  assert.equal(d.querySelectorAll('.discovery-result').length,1,'Console filter narrows the list');
  d.getElementById('discovery-origin').value='';d.getElementById('discovery-origin').dispatchEvent(new w.Event('change'));await tick();await tick();
  [...d.querySelectorAll('.discovery-result')][0].querySelector('.text-button').click();await tick();await tick();
  assert(d.getElementById('discovery-romm-banjo').textContent.includes('You have this game in RomM'));
  assert(d.querySelector('.discovery-result').textContent.includes('Packaging steps available'));
  const discoverySearch=d.getElementById('discovery-search');discoverySearch.focus();discoverySearch.value='new';discoverySearch.dispatchEvent(new w.Event('input'));discoverySearch.setSelectionRange(1,3);
  await new Promise(r=>setTimeout(r,350));await tick();
  assert.equal(d.querySelectorAll('.discovery-result').length,1);
  [...d.querySelectorAll('#game-discovery button')].find(n=>n.textContent==='Refresh catalog').click();await tick();await tick();
  assert.equal(d.activeElement.id,'discovery-search');assert.equal(d.activeElement.value,'new');assert.equal(d.activeElement.selectionStart,1);assert.equal(d.activeElement.selectionEnd,3,'Refreshing retains the search selection');
  d.querySelector('.discovery-result .button').click();await tick();await tick();
  assert(requests.at(-1).endpoint==='/api/discovery/resolve');
  assert.equal(d.getElementById('custom-repo').value,'example/new-port');
  assert(d.getElementById('custom-target-windows').checked);assert(!d.getElementById('custom-target-linux').checked,'Platforms come from upstream lookup, not reference text');
  assert(d.getElementById('add-game-content').textContent.includes('Found on Recomp Launcher'));
  const changedRepo=d.getElementById('custom-repo');changedRepo.value='example/different';changedRepo.dispatchEvent(new w.Event('input'));
  assert(d.getElementById('custom-add-submit').disabled,'Editing a resolved repository invalidates the previous lookup');
  dom.window.close();
  discoveryFixture.discovery_resolution={url:'https://recomplauncher.com/games/new-port',repositories:[{repository:'example/new-port'},{repository:'example/extraction-tool'}]};
  dom=await create(null,false,discoveryFixture);w=dom.window;d=w.document;
  d.getElementById('add-project').click();await tick();await tick();
  [...d.querySelectorAll('#add-game-content button')].find(n=>n.textContent==='Browse all Recomp games').click();await tick();await tick();
  [...d.querySelectorAll('.discovery-result')].find(n=>n.textContent.includes('New port')).querySelector('.button').click();await tick();await tick();
  assert(d.getElementById('add-game-content').textContent.includes('several repositories'));
  assert(!d.getElementById('custom-add-submit'),'An ambiguous page cannot create a game until a repository is chosen');
  [...d.querySelectorAll('#add-game-content button')].find(n=>n.textContent==='example/new-port').click();await tick();await tick();
  assert.equal(d.getElementById('custom-repo').value,'example/new-port');assert(d.getElementById('custom-add-submit'));
  dom.window.close();
  dom=await create(null,false,discoveryFixture);w=dom.window;d=w.document;
  d.getElementById('add-project').click();await tick();await tick();[...d.querySelectorAll('#add-game-content button')].find(n=>n.textContent==='Browse all Recomp games').click();await tick();await tick();
  [...d.querySelectorAll('.discovery-result')].find(n=>n.textContent.includes('Banjo Recompiled')).querySelector('.button').click();await tick();await tick();
  assert(d.getElementById('add-game-content').textContent.includes('verified as USA 1.0'),'A known discovery repository uses its existing catalog file-selection flow');
  assert(d.getElementById('add-game-submit'),'Known projects do not need a model just to select game files');
  dom.window.close();
  discoveryFixture.discovery={count:0,stale:true,refreshing:false,error:'Could not refresh the Recomp catalog. The last saved copy is still available.'};discoveryFixture.discovery_games=[];
  dom=await create(null,false,discoveryFixture);w=dom.window;d=w.document;
  d.getElementById('add-project').click();await tick();await tick();[...d.querySelectorAll('#add-game-content button')].find(n=>n.textContent==='Browse all Recomp games').click();await tick();await tick();
  assert(d.getElementById('game-discovery').textContent.includes('Could not refresh'));
  assert(d.querySelector('a[href="https://recomplauncher.com"]'),'The reference website stays accessible when the catalog is unavailable');
  assert(d.querySelector('.game-pick.custom'),'Manual addition stays available');dom.window.close();
  // A slow older search response must not replace newer results.
  const gates={};
  discoveryFixture.discovery={source:'Recomp Launcher',count:2,updated:'2026-11-03',stale:false,refreshing:false};
  discoveryFixture.discovery_games=[
    {id:'banjo',name:'Banjo Recompiled',origin:'n64',repository:'BanjoRecomp/BanjoRecomp',systems:['windows'],has_cover:false,in_library:null,known:true},
    {id:'zelda',name:'Zelda Port',origin:'n64',repository:'example/zelda',systems:['windows'],has_cover:false,in_library:null,known:false}];
  dom=await create(null,false,discoveryFixture);w=dom.window;d=w.document;
  d.getElementById('add-project').click();await tick();await tick();[...d.querySelectorAll('#add-game-content button')].find(n=>n.textContent==='Browse all Recomp games').click();await tick();await tick();
  discoveryFixture.games_gate=q=>q.get('q')==='ban'?new Promise(resolve=>{gates.ban=resolve;}):null;
  const raceSearch=d.getElementById('discovery-search');
  raceSearch.value='ban';raceSearch.dispatchEvent(new w.Event('input'));await new Promise(r=>setTimeout(r,250));
  raceSearch.value='zel';raceSearch.dispatchEvent(new w.Event('input'));await new Promise(r=>setTimeout(r,250));await tick();
  assert(gates.ban,'The older search request is pending');
  assert.deepEqual([...d.querySelectorAll('.discovery-result')].map(n=>n.textContent.includes('Zelda Port')),[true]);
  gates.ban();await tick();await tick();
  assert.equal(d.querySelectorAll('.discovery-result').length,1,'A late older response is discarded');
  assert(d.querySelector('.discovery-result').textContent.includes('Zelda Port'),'The newest search results stay on screen');
  delete discoveryFixture.games_gate;dom.window.close();
  // Settings → Catalog card: status load, refresh, button state and error display.
  discoveryFixture.discovery={source:'Recomp Launcher',count:2,updated:'2026-11-03',skipped:1,stale:false,refreshing:false};
  dom=await create(null,false,discoveryFixture);w=dom.window;d=w.document;
  const catalogStatus=()=>d.getElementById('recomp-catalog-status').textContent;
  const catalogBefore=requests.length;
  d.querySelector('[data-settings-tab="catalog"]').click();await tick();await tick();
  assert.equal(requests.slice(catalogBefore).filter(r=>r.endpoint==="/api/discovery").length,1,'Opening the tab loads the status once');
  assert(catalogStatus().includes('Recomp catalog: 2 games, updated 2026-11-03'));
  assert(catalogStatus().includes('1 malformed entries were ignored'));
  assert.equal(d.getElementById('recomp-catalog-refresh').disabled,false);
  d.getElementById('recomp-catalog-refresh').click();
  assert.equal(d.getElementById('recomp-catalog-refresh').disabled,true,'Refresh is disabled while running');
  await tick();await tick();
  assert.deepEqual(requests.at(-1),{endpoint:'/api/discovery/refresh',body:{force:true}});
  assert.equal(d.getElementById('recomp-catalog-refresh').disabled,false,'Refresh is re-enabled afterwards');
  dom.window.close();
  discoveryFixture.discovery={count:0,stale:true,refreshing:false,error:'Could not refresh the Recomp catalog. The last saved copy is still available.'};
  dom=await create(null,false,discoveryFixture);w=dom.window;d=w.document;
  d.querySelector('[data-settings-tab="catalog"]').click();await tick();await tick();
  assert(d.getElementById('recomp-catalog-status').textContent.includes('not saved yet'));
  assert(d.querySelector('#recomp-catalog-status .error').textContent.includes('Could not refresh'),'The last error is shown');
  dom.window.close();
  dom=await create(null,false,{...fixture,projects:[],artifacts:[]});assert(dom.window.document.getElementById('project-grid').textContent.includes('first game'));dom.window.close();
  // First-run guidance: broker explanation, checklist, review panel, inline progress.
  dom=await create();w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();
  let stepCard=d.querySelector('.next-step-card');
  assert(!stepCard.textContent.includes('Investigate packaging'),'Without a worker broker the page does not offer automatic investigation');
  assert(stepCard.textContent.includes('worker broker'),'The page explains why automatic setup is unavailable');
  assert([...stepCard.querySelectorAll('button')].some(n=>n.textContent==='Draft packaging steps'),'The working draft path is offered instead');
  const checklist=[...stepCard.querySelectorAll('.game-checklist li')];
  assert.deepEqual(checklist.map(n=>n.dataset.step),['saved','drafted','approved','built','tested','in_romm']);
  assert.equal(stepCard.querySelectorAll('.game-checklist li[data-state="current"]').length,1,'Exactly one step is current');
  assert.equal(stepCard.querySelector('.game-checklist li[data-state="current"]').dataset.step,'saved','The fixture game has no saved release yet');
  {
    // Checklist states: preserve-only, pending draft, approved, built, built and tested.
    const states=(project,proposal,packages,inRomm,untested)=>Object.fromEntries([...w.__gameChecklist(project,proposal,packages,inRomm,untested).querySelectorAll('li')].map(n=>[n.dataset.step,n.dataset.state]));
    const saved={latest_version:'v1',capture_id:'c1'}, preserveOnly={...saved,packaging:{runner:'capture',revision:1}}, approvedDef={...saved,packaging:{runner:'operations',revision:2}};
    const pkg={id:'a1',target:'linux'};
    assert.deepEqual(states(preserveOnly,null,[],[],[]),{saved:'done',drafted:'current',approved:'todo',built:'todo',tested:'todo',in_romm:'todo'},'Preserve-only is not drafted or approved');
    assert.deepEqual(states(preserveOnly,{id:'p1'},[],[],[]),{saved:'done',drafted:'done',approved:'current',built:'todo',tested:'todo',in_romm:'todo'},'A pending draft is drafted but not approved');
    assert.deepEqual(states(approvedDef,null,[],[],[]),{saved:'done',drafted:'done',approved:'done',built:'current',tested:'todo',in_romm:'todo'},'Approved definition not yet built');
    assert.deepEqual(states(approvedDef,null,[pkg],[],[pkg]),{saved:'done',drafted:'done',approved:'done',built:'done',tested:'current',in_romm:'todo'},'Built but untested');
    assert.deepEqual(states(approvedDef,null,[pkg],[],[]),{saved:'done',drafted:'done',approved:'done',built:'done',tested:'done',in_romm:'current'},'Built and tested');
  }
  d.getElementById('project-advanced-tab').click();await tick();
  const autoCard=d.querySelector('.automatic-setup');
  assert(autoCard.textContent.includes('worker broker') && autoCard.textContent.includes('Draft packaging steps'));
  assert(![...autoCard.querySelectorAll('button')].some(n=>n.textContent==='Set up automatically'),'No greyed automatic setup button');
  [...autoCard.querySelectorAll('button')].find(n=>n.textContent.startsWith('Go to worker broker settings')).click();await tick();
  assert.equal(d.getElementById('settings-storage-tab').getAttribute('aria-selected'),'true','Link lands on Advanced > Storage & runners');
  assert(d.getElementById('automatic-settings').textContent.includes('What is a worker broker'),'The settings section defines the broker');
  dom.window.close();
  dom=await create(null,false,reviewed);w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();
  [...d.querySelector('.next-step-card').querySelectorAll('button')].find(n=>n.textContent==='Review draft').click();await tick();
  assert(d.getElementById('draft-review-dialog').open,'Review draft opens a panel');
  assert(w.location.hash.endsWith('/overview'),'The page stays on Play');
  const reviewBody=d.getElementById('draft-review-body'),reviewActions=d.getElementById('draft-review-actions');
  assert(reviewBody.textContent.includes('Review it before approving'));
  assert([...reviewActions.querySelectorAll('button')].map(n=>n.textContent).join('|').includes('Approve'));
  assert([...reviewActions.querySelectorAll('button')].some(n=>n.textContent==='Discard'));
  assert(!reviewBody.querySelector('button.primary'),'Approve is pinned outside the scrolling body');
  assert([...reviewBody.querySelectorAll('details')].every(n=>!n.open),'JSON stays collapsed');
  [...reviewActions.querySelectorAll('button')].find(n=>n.textContent==='Approve').click();await tick();await tick();
  assert(!d.getElementById('draft-review-dialog').open,'Approving closes the panel');
  dom.window.close();
  // Review investigation opens the same dialog; Inbox "Open game" lands on it too.
  const planned=JSON.parse(JSON.stringify(editable));planned.projects[1].capture_id=id(88);planned.assistant={provider:'ollama',configured:true,drafting_model:'Local fixture'};
  planned.proposals=[{id:id(91),kind:'investigation',project_id:id(2),status:'pending_review',prepare_game_files:true,source:'Local fixture',plan:{outcome:'ready',summary:'A supported runtime is available.',targets:[{target:'windows',method:'release',asset:'game-windows.zip'}],game_files:{needed_to_play:true,needed_for_packaging:false,requirements:'Your owned game data'},steps:[{capability:'extract',purpose:'Unpack the release'}],evidence:[{source:'e2',path:'README.md',quote:'Windows build',supports:'Runtime',matched:true}],uncertainties:[],capability_problems:[],can_draft:true}}];
  planned.notifications=[{id:'review-note',project_id:id(2),kind:'needs_review',message:'New version',created_at:'2026-10-13T05:00:00Z'}];
  for(const entry of ['card','inbox']){
    dom=await create(null,false,planned);w=dom.window;d=w.document;
    [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();
    if(entry==='card')[...d.querySelector('.next-step-card').querySelectorAll('button')].find(n=>n.textContent==='Review investigation').click();
    else{d.getElementById('activity-tab')?.click?.();w.location.hash='activity';await tick();d.getElementById('notifications-tab')?.click();await tick();[...d.querySelectorAll('.notification-item button')].find(n=>n.textContent.startsWith('Open game')).click();}
    await tick();
    const dialog=d.getElementById('draft-review-dialog');assert(dialog.open,'Review investigation opens the dialog from '+entry);
    assert(w.location.hash.endsWith('/overview'),'The page stays on Play');
    const body=d.getElementById('draft-review-body'),pinned=d.getElementById('draft-review-actions');
    assert(body.textContent.includes('A supported runtime is available.'));
    assert(!body.querySelector('button.primary'),'Actions are pinned outside the scrolling body');
    assert.deepEqual([...pinned.querySelectorAll('button')].map(n=>n.textContent),['Draft packaging steps from this plan','Dismiss']);
    assert([...body.querySelectorAll('details')].every(n=>!n.open),'Evidence and JSON stay collapsed');
    if(entry==='card'){[...pinned.querySelectorAll('button')].find(n=>n.textContent==='Dismiss').click();await tick();await tick();assert(!dialog.open,'Dismissing closes the dialog');}
    else{dialog.querySelector('[data-close]').click();assert(!dialog.open,'Closing returns to the game');}
    dom.window.close();
  }
  // Silent-draft case: a failed "Draft packaging steps from this plan" shows a persistent inline message with a fix.
  planned.draft_error='Release, selected platforms or build instructions changed; investigate again before drafting';
  dom=await create(null,false,planned);w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();
  [...d.querySelector('.next-step-card').querySelectorAll('button')].find(n=>n.textContent==='Review investigation').click();await tick();
  const reviewDialog=d.getElementById('draft-review-dialog');
  assert.equal(reviewDialog.getAttribute('role'),'dialog');assert(reviewDialog.getAttribute('aria-labelledby'));
  assert.equal(reviewDialog.getAttribute('aria-modal'),'true');
  [...d.getElementById('draft-review-actions').querySelectorAll('button')].find(n=>n.textContent==='Draft packaging steps from this plan').click();await tick();await tick();
  assert(reviewDialog.open,'A failed draft keeps the review open');
  const inlineError=reviewDialog.querySelector('[role="alert"].action-error');
  assert(inlineError && inlineError.textContent.includes('investigate again before drafting'),'The error is shown inline');
  assert([...inlineError.querySelectorAll('button')].some(n=>n.textContent==='Investigate again'),'The inline error offers the fix');
  await new Promise(resolve=>setTimeout(resolve,100));
  assert(reviewDialog.querySelector('.action-error'),'The inline error persists');
  [...inlineError.querySelectorAll('button')].find(n=>n.textContent==='Dismiss').click();await tick();
  assert(!reviewDialog.querySelector('.action-error'),'Dismissing removes the error');
  assert.equal(w.portkeeperFormat.localizeDates('Drafted by Local on 2026-11-03T01:30:00+00:00 for release x; Applied on 2026-11-03.'),'Drafted by Local on Nov 2, 2026, 8:30 PM for release x; Applied on Nov 3, 2026.');
  dom.window.close();
  const css=fs.readFileSync(path.join(base,'static/styles.css'),'utf8');
  assert(!/\.draft-review-dialog\{[^}]*display:flex/.test(css),'A closed review dialog is not rendered in the page flow');
  assert(/dialog\[open\]\{[^}]*position:fixed/.test(css),'Open dialogs are fixed to the viewport');
  for(const [commit,expected] of [[null,'v0.3.0 (dev)'],['abc1234def5678','v0.3.0 (abc1234)']]){
    const versioned=JSON.parse(JSON.stringify(fixture));versioned.version_info={version:'0.3.0',commit};
    dom=await create(null,false,versioned);w=dom.window;d=w.document;await tick();
    assert.equal(d.getElementById('app-version').textContent,expected,'Footer build label');
    assert.equal(d.getElementById('about-version').textContent,expected,'Settings build label');
    dom.window.close();
  }
  const building=JSON.parse(JSON.stringify(fixture));
  building.jobs=[{id:id(40),project_id:id(2),status:'packaging_linux',stage:'packaging_linux',target:'linux',kind:'build',created_at:'2026-10-11T12:00:00Z',logs:['Packaging the Linux build']}];
  dom=await create(null,false,building);w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Bloodborne')).click();await tick();
  stepCard=d.querySelector('.next-step-card');
  assert(stepCard.querySelector('progress[role="status"], [role="progressbar"], progress'),'Progress is drawn on the card where the build was started');
  assert(stepCard.textContent.includes('Packaging the Linux build'),'The latest log line is shown inline');
  dom.window.close();
  dom=await create();w=dom.window;d=w.document;
  d.getElementById('add-project').click();await tick();await tick();d.querySelector('.game-pick.custom').click();await tick();[...d.querySelectorAll('#add-game-actions button')].find(n=>n.textContent.includes('Continue')).click();await tick();
  const landRepo=d.getElementById('custom-repo');landRepo.value='example/game';landRepo.dispatchEvent(new w.Event('input'));landRepo.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));await tick();await tick();
  d.getElementById('custom-method-handoff').click();d.getElementById('custom-method-handoff').dispatchEvent(new w.Event('change',{bubbles:true}));await tick();
  d.getElementById('custom-add-submit').click();await tick();await tick();await tick();
  assert(w.location.hash.endsWith('/overview'),'After adding a custom game the app lands on Play, not Advanced');
  dom.window.close();
  dom=await create();w=dom.window;
  const raw='Investigating. 2026-11-03T03:03:21.822778+00:00 Reading the release; 2026-11-03T03:04:15Z done';
  assert.strictEqual(w.portkeeperFormat.localTime('2026-11-03T03:03:21.822778+00:00').replace(/\s/g,' '),'10:03 PM','UTC is shown in the browser local time');
  const localized=w.portkeeperFormat.localizeTimestamps(raw);
  assert(!/\d{4}-\d{2}-\d{2}T/.test(localized),'No raw ISO timestamp remains: '+localized);
  assert(/Investigating\. 10:03.PM Reading the release; 10:04.PM done/.test(localized),localized);
  assert.strictEqual(w.portkeeperFormat.localizeTimestamps('no time here'),'no time here');
  dom.window.close();
  // First-run empty states: each says what to do next and links to the Settings section that fixes it.
  const blank=(extra={})=>({...JSON.parse(JSON.stringify(fixture)),projects:[],artifacts:[],romm:{},assistant:{},automation:{},summary:{projects:0,artifacts:0},...extra});
  const bare=blank();
  dom=await create(null,false,bare);w=dom.window;d=w.document;
  let empty=d.getElementById('library-empty');
  assert(empty,'A fresh install shows a first-run panel');
  assert(empty.textContent.includes('Connect RomM first'),'One sentence says what to do first');
  const primary=empty.querySelector('.button.primary');assert.equal(primary.textContent,'Connect RomM');
  assert(d.getElementById('library-summary-counts').hidden,'No bare "0 games, 0 platform downloads, 0 need attention"');
  assert(!d.getElementById('summary-empty').hidden&&d.getElementById('summary-empty').textContent.includes('Connect RomM'));
  assert(empty.textContent.includes('drafting assistant')&&empty.textContent.includes('worker broker'),'Optional pieces are explained as optional');
  primary.click();await new Promise(r=>setTimeout(r,120));
  assert.equal(w.location.hash,'#settings');assert(!d.getElementById('settings-romm').hidden,'Connect RomM lands on the RomM section');
  assert.equal(d.activeElement.id,'romm-url','and focuses the server URL field');
  dom.window.close();
  const rommOnly=blank({romm:{url:'https://romm.example',has_token:true,configured:true,connected:true}});
  dom=await create(null,false,rommOnly);w=dom.window;d=w.document;
  empty=d.getElementById('library-empty');
  assert(empty.querySelector('h3').textContent==='Add your first game');
  assert.equal(empty.querySelector('.button.primary').textContent,'Add your first game');
  assert(d.getElementById('library-summary-counts').hidden);assert(d.getElementById('summary-empty').textContent.includes('Add your first game'));
  empty.querySelector('.button.primary').click();await tick();await tick();assert(d.getElementById('add-game-dialog').open,'The primary action opens Add a game');
  dom.window.close();
  const noAssistant=blank({romm:{url:'https://romm.example',has_token:true,configured:true,connected:true},no_imports:true,
    projects:[{id:id(9),name:'Perfect Dark',repo:'example/pd',targets:['linux'],profile:null,recipe:{mode:'capture'},status:'archived',metadata:{},poll_minutes:360}],
    assistant:{provider:'codex',provider_label:'Codex',configured:false,drafting_model:'Codex default',agents:{codex:{installed:true,authenticated:false}}}});
  dom=await create(null,false,noAssistant);w=dom.window;d=w.document;await tick();await tick();
  const banner=d.getElementById('library-next-step');
  assert(!banner.hidden&&banner.textContent.includes('drafting assistant'),'Games without an assistant get a one-sentence next step');
  assert(!d.getElementById('library-summary-counts').hidden,'Counters return once there are games');
  banner.querySelector('button').click();await tick();await tick();
  assert(!d.getElementById('settings-assistant').hidden,'The banner lands on Drafting assistant settings');
  w.location.hash='';await tick();d.getElementById('add-project').click();await tick();await tick();d.querySelector('.game-pick.custom').click();await tick();[...d.querySelectorAll('#add-game-actions button')].find(n=>n.textContent.includes('Continue')).click();await tick();
  const landRepo2=d.getElementById('custom-repo');landRepo2.value='example/game';landRepo2.dispatchEvent(new w.Event('input'));landRepo2.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));await tick();await tick();
  const unavailable=d.querySelector('#add-game-content .choice-card.disabled');
  assert(unavailable&&unavailable.textContent.includes('Draft with Codex default')&&unavailable.textContent.includes('isn’t signed in'),'The unavailable default choice explains what is needed before it fails');
  assert(!d.getElementById('custom-method-assistant'),'It cannot be chosen');
  dom.window.close();
  // Not connected but games exist: banner offers Connect RomM.
  const noRomm=blank({projects:[noAssistant.projects[0]],no_imports:true});
  dom=await create(null,false,noRomm);w=dom.window;d=w.document;
  assert(d.getElementById('library-next-step').textContent.includes('Connect RomM'));
  dom.window.close();
  // Covers: user-chosen art wins, otherwise the RomM match is requested; no cover shows initials.
  const covers=blank({romm:{url:'https://romm.example',has_token:true,configured:true,connected:true},no_imports:true,assistant:{configured:true},projects:[
    {id:id(21),name:'Perfect Dark',repo:'e/a',targets:['linux'],recipe:{mode:'capture'},status:'archived',metadata:{}},
    {id:id(22),name:'Super Metroid',repo:'e/b',targets:['linux'],recipe:{mode:'capture'},status:'archived',metadata:{cover_policy:'custom',url_cover:'https://cdn.example/mine.png',romm_rom_id:5}},
    {id:id(23),name:'Donkey Kong 64',repo:'e/c',targets:['linux'],recipe:{mode:'capture'},status:'archived',metadata:{}}]});
  dom=await create(null,false,covers);w=dom.window;d=w.document;
  const thumbs=[...d.querySelectorAll('.workspace-game-button')];
  const art=name=>thumbs.find(n=>n.textContent.includes(name)).querySelector('.workspace-game-cover');
  assert(art('Perfect Dark').src.includes('rom=match'),'No link: ask the server for the RomM match');
  assert(art('Super Metroid').src.includes('art='),'A user-chosen cover is requested as the user\'s own, not the RomM one');
  art('Donkey Kong 64').dispatchEvent(new w.Event('error'));
  const tile=thumbs.find(n=>n.textContent.includes('Donkey Kong 64')).querySelector('.cover-placeholder');
  assert(tile&&tile.textContent==='DK','A game with no cover gets a placeholder with its initials');
  thumbs.find(n=>n.textContent.includes('Perfect Dark')).click();await tick();
  const sourceNote=d.getElementById('cover-source-note');assert(sourceNote.hidden,'No "from RomM" claim before the cover has loaded');
  d.querySelector('.project-hero-cover').dispatchEvent(new w.Event('load'));
  assert(!sourceNote.hidden&&sourceNote.textContent.includes('from RomM'),'A RomM cover is labelled');
  assert([...d.querySelectorAll('button')].some(n=>n.textContent==='Change cover art'),'with a one-click way to replace it');
  thumbs.find(n=>n.textContent.includes('Super Metroid')).click();await tick();
  d.querySelector('.project-hero-cover').dispatchEvent(new w.Event('load'));
  assert(d.getElementById('cover-source-note').hidden,'A user-chosen cover is never labelled as RomM');
  dom.window.close();
  // Issue #82: phone list/detail, hidden games, unknown rows, cover misses, pluralization, one count, step labels.
  const romm={url:'https://romm.example',has_token:true,configured:true,connected:true};
  const lib=(n,name,extra={})=>({id:id(n),name,repo:'e/'+n,targets:['linux'],recipe:{mode:'capture'},status:'archived',metadata:{},...extra});
  const mixed=blank({romm,no_imports:true,assistant:{configured:true},projects:[
    lib(31,'Zelda'),lib(32,'Starship test'),lib(33,'sm64coopdx automation acceptance'),lib(34,'gow3_pc',{metadata:{game:'God of War III'}}),
    lib(35,'Manual test',{hidden:false}),lib(36,'Plain',{hidden:true})]});
  dom=await create(null,false,mixed);w=dom.window;d=w.document;
  const listed=()=>[...d.querySelectorAll('.workspace-game-button strong')].map(n=>n.textContent);
  assert.deepStrictEqual(listed(),['God of War III','Manual test','Zelda'],'Test games are hidden by default; an explicit choice wins; the display title replaces the slug');
  assert(!d.getElementById('library-hidden-field').hidden&&d.getElementById('library-hidden-count').textContent==='(3)','Include hidden shows how many are hidden');
  assert.equal(d.getElementById('summary-projects').textContent,'3');
  const includeHidden=d.getElementById('library-hidden');includeHidden.checked=true;includeHidden.dispatchEvent(new w.Event('change'));
  assert.deepStrictEqual(listed(),['God of War III','Manual test','Plain','sm64coopdx automation acceptance','Starship test','Zelda'],'Include hidden brings every game back');
  assert.equal(mixed.projects.length,6,'Nothing is deleted');
  d.querySelector('.workspace-game-button').click();await tick();
  assert(d.getElementById('project-heading').textContent.includes('God of War III'),'The game page uses the display title');
  w.location.hash='#project/'+id(36)+'/advanced';await tick();await tick();
  const advanced=d.getElementById('project-content').textContent;
  assert(advanced.includes('Project name: Plain'),'The project name stays in Advanced');
  const hideButton=[...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='Show in the main library');
  assert(hideButton,'A hidden game offers to come back');
  hideButton.click();await tick();
  dom.window.close();
  // Hiding calls the API and does not remove the game.
  const hideCalls=[];
  dom=await create(null,false,mixed);w=dom.window;d=w.document;
  const realFetch=w.fetch;w.fetch=async(endpoint,options={})=>{if(endpoint.endsWith('/hidden')){hideCalls.push({endpoint,body:JSON.parse(options.body)});return {ok:true,status:200,json:async()=>({})};}return realFetch(endpoint,options);};
  w.location.hash='#project/'+id(31)+'/advanced';await tick();await tick();
  [...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='Mark as test / hidden game').click();await tick();
  assert.deepStrictEqual(hideCalls,[{endpoint:`/api/projects/${id(31)}/hidden`,body:{hidden:true}}]);
  dom.window.close();
  // Phone: the route decides whether the list or the game shows, and Back returns to the same scroll position.
  dom=await create(null,false,mixed);w=dom.window;d=w.document;
  const scrolls=[];w.scrollTo=(x,y)=>scrolls.push(y);
  assert.equal(d.getElementById('library-page').dataset.mobileView,'list','The library route shows the list');
  const back=d.querySelector('.workspace-game-main .mobile-back');
  assert(back&&back.textContent==='← All games'&&back.getAttribute('href')==='#library','A visible All games control sits above the game');
  Object.defineProperty(w,'scrollY',{value:640,configurable:true});
  d.querySelector('.workspace-game-button').click();await tick();await tick();
  assert.equal(d.getElementById('library-page').dataset.mobileView,'game','Opening a game shows the game, not the list');
  assert.equal(scrolls.at(-1),0,'and starts at its top');
  Object.defineProperty(w,'scrollY',{value:0,configurable:true});
  w.location.hash='#library';await tick();await tick();
  assert.equal(d.getElementById('library-page').dataset.mobileView,'list');
  assert.equal(scrolls.at(-1),640,'Back to the list restores its scroll position');
  assert(stylesheet.includes('[data-mobile-view="game"] .workspace-game-list{display:none}')&&stylesheet.includes('[data-mobile-view="list"] .workspace-game-main{display:none}'),'Narrow widths hide whichever of list and game is not chosen');
  assert(/@media\(max-width:800px\)\{[^@]*\.mobile-back\{display:inline-flex/.test(stylesheet.replace(/\n/g,'')),'The All games control only shows on phones');
  dom.window.close();
  // Unknown rows are hidden; a block with nothing known is not shown.
  const known=JSON.parse(JSON.stringify(fixture));
  known.artifacts[0].manifest={version:'9.3',package_revision:2,changes:'Fixed the launcher.'};
  dom=await create(null,false,known);w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Ship')).click();await tick();await tick();
  let details=d.querySelector('.download-details').textContent;
  assert(details.includes('Package revision 2')&&details.includes('What changed: Fixed the launcher.'),'Known values still show: '+details);
  assert(!/Unknown/.test(details)&&!details.includes('Requirements:')&&!details.includes('Known issues:'),'Unknown rows are hidden: '+details);
  dom.window.close();
  const blankDetails=JSON.parse(JSON.stringify(fixture));blankDetails.artifacts[0].version='';
  dom=await create(null,false,blankDetails);w=dom.window;d=w.document;
  [...d.querySelectorAll('.workspace-game-button')].find(n=>n.textContent.includes('Ship')).click();await tick();await tick();
  assert(!/Unknown|not recorded for this download/.test(d.querySelector('.download-details').textContent)&&!d.querySelector('.package-revision'),'With nothing known there is no revision line and no placeholder rows');
  dom.window.close();
  // Missing covers are requested once, remembered, and shown as a placeholder without re-requesting.
  const missing=blank({romm,no_imports:true,assistant:{configured:true},projects:[lib(41,'Perfect Dark')]});
  dom=await create(null,false,missing);w=dom.window;d=w.document;
  let tileCovers=()=>[...d.querySelectorAll('img[src*="/cover?"]')];
  assert.equal(tileCovers().length>0,true,'The first load asks for the cover');
  tileCovers().forEach(n=>n.dispatchEvent(new w.Event('error')));
  assert.equal(tileCovers().length,0,'A miss is replaced by the placeholder');
  assert(w.localStorage.getItem('portkeeper.cover-misses').includes(id(41)),'The miss is remembered across page loads');
  w.pollRefresh?.();w.location.hash='#project/'+id(41)+'/overview';await tick();await tick();
  assert.equal(tileCovers().length,0,'Re-rendering and opening the game do not request it again');
  assert(d.querySelector('.project-hero-cover.cover-placeholder'),'The game page shows the placeholder');
  const misses=JSON.parse(w.localStorage.getItem('portkeeper.cover-misses'));
  const missKey=Object.keys(misses)[0];
  dom.window.close();
  // A miss older than an hour is retried; a fresh one is not.
  dom=await create(null,false,missing,{'portkeeper.cover-misses':JSON.stringify({[missKey]:Date.now()-5*60*1000})});w=dom.window;d=w.document;
  assert.equal(tileCovers().length,0,'A recent miss from an earlier page load is not requested again');
  assert(d.querySelector('.workspace-game-cover.cover-placeholder'),'and shows the placeholder straight away');
  dom.window.close();
  dom=await create(null,false,missing,{'portkeeper.cover-misses':JSON.stringify({[missKey]:Date.now()-2*60*60*1000})});w=dom.window;d=w.document;
  assert(tileCovers().length>0,'A miss older than an hour is tried again');
  dom.window.close();
  // Pluralization and a single notification count.
  const one=blank({romm,no_imports:true,assistant:{configured:true},projects:[lib(51,'Solo',{last_error:'boom'})],notifications:[{id:'n1',title:'x',unread:true},{id:'n2',title:'y',unread:true}]});
  dom=await create(null,false,one);w=dom.window;d=w.document;
  assert.equal(d.getElementById('attention-summary').textContent.trim(),'1 needs attention','One game needs attention');
  assert.equal(d.getElementById('summary-projects-label').textContent,'game','1 game is singular');assert.equal(d.getElementById('summary-artifacts-label').textContent,'platform downloads','0 downloads is plural');
  assert(!d.getElementById('nav-notification-count'),'The sidebar no longer repeats the inbox count');
  assert.equal(d.getElementById('inbox-count').textContent,'2','The count lives in the Inbox button only');
  dom.window.close();
  const two=blank({romm,no_imports:true,assistant:{configured:true},projects:[lib(52,'A',{last_error:'x'}),lib(53,'B',{last_error:'y'})]});
  dom=await create(null,false,two);w=dom.window;d=w.document;
  assert.equal(d.getElementById('attention-summary').textContent.trim(),'2 need attention');
  dom.window.close();
  // Plain-language progress steps with a tooltip each.
  dom=await create();w=dom.window;d=w.document;
  d.querySelector('.workspace-game-button').click();await tick();await tick();
  const steps=[...d.querySelectorAll('.game-checklist li')];
  assert.deepStrictEqual(steps.map(n=>n.textContent.replace(/^[^A-Za-z]+/,'')),['Downloaded','Setup written','Approved','Built','Tested','In RomM']);
  assert(steps.every(n=>n.title.length>10),'Every step explains itself in a tooltip');
  dom.window.close();
  assert(stylesheet.includes('@media(min-width:761px){.app-shell{background:linear-gradient'),'The sidebar colour fills the full page height');
  console.log('UI layouts passed: add a game, next step, drafting settings, default workspace, saved preferences, collection/list switching, grouped platforms, RomM imports, correct counts, game selection, deep links, filters, empty state and blocked storage.');
})().catch(error=>{console.error(error);process.exitCode=1;});
