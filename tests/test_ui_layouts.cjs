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
async function create(saved, storageBlocked=false, source=fixture) {
  const errors=[];
  const dom=new JSDOM(fs.readFileSync(path.join(base,'static/index.html'),'utf8'),{url:'http://localhost:8787',runScripts:'outside-only',pretendToBeVisual:true});
  const w=dom.window;w.scrollTo=()=>{};w.setInterval=callback=>{w.pollRefresh=callback;return 0;};w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;};
  if(saved)w.localStorage.setItem('portkeeper.library-layout',saved);
  if(storageBlocked)Object.defineProperty(w,'localStorage',{get(){throw new Error('Storage unavailable');}});
  w.addEventListener('error',e=>errors.push(e.error));
  w.fetch=async (endpoint, options={})=>{
    let payload={};
    if(endpoint==='/api/state')payload=source;
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
    else if(endpoint.match(/^\/api\/projects\/[^/]+\/imports$/)){const game=source.projects.find(p=>endpoint.includes(p.id));payload={items:imports(game.metadata.filename_prefix)};}
    else if(endpoint.startsWith('/api/romm/search?')){const prefix=new URL(endpoint,w.location.href).searchParams.get('q');payload={items:imports(prefix)};}
    else if(endpoint.startsWith('/preservation',0)||endpoint.includes('/preservation'))payload={};
    else if(endpoint==='/api/profiles')payload={profiles:[]};
    else if(endpoint==='/api/recipes')payload={recipes:[]};
    else if(endpoint.startsWith('/api/github/lookup?'))payload=source.platform_lookup || {repository:'example/game',version:'v1',assets:['game-linux.zip','game-windows.zip'],platforms:['linux','windows'],platform_detection:{suggested_targets:['linux','windows'],platforms:[{id:'linux',label:'Linux',packaging_supported:true,evidence:[{kind:'release',detail:'game-linux.zip',source:'Release files'}]},{id:'windows',label:'Windows',packaging_supported:true,evidence:[{kind:'documentation',detail:'Windows build instructions',source:'README.md'}]},{id:'android',label:'Android',packaging_supported:false,evidence:[{kind:'documentation',detail:'Android build instructions',source:'README.md'}]}]}};
    else if(endpoint==='/api/catalog/games')payload={games:source.catalog_games || [{id:'banjo-recompiled',name:'Banjo Recompiled',targets:['linux','windows'],runner:'operations',needs:[{label:'Banjo-Kazooie (USA) 1.0 N64 ROM'}],first_run:{mode:'preloaded',text:'Your ROM is placed where Banjo looks for it.'},in_library:null}],romm_connected:true};
    else if(endpoint==='/api/catalog/games/banjo-recompiled/files')payload={inputs:[{id:'rom',label:'Banjo-Kazooie (USA) 1.0 N64 ROM',kind:'file',required:true,accepted:['USA 1.0'],found:[{path:'n64/roms/Banjo-Kazooie (USA).z64',name:'Banjo-Kazooie (USA).z64',accepted:'USA 1.0'}],rejected:[]}]};
    else if(endpoint.endsWith('/configuration/validate')){requests.push({endpoint,body:JSON.parse(options.body)});payload={valid:true};}
    else if(endpoint.match(/^\/api\/projects\/[^/]+\/inputs$/)){
      if(options.method==='POST'){const body=JSON.parse(options.body);requests.push({endpoint,body});const game=source.projects.find(p=>endpoint.includes(p.id));game.input_bindings||={};game.input_bindings[body.input]={source:body.source||'inputs',path:body.path,ps4:{title_id:'CUSA01623',app_version:body.input==='game_pkg'?'01.00':'01.02',role:body.input==='game_pkg'?'base':'update'}};payload=game;}
      else payload=source.input_choices||{inputs:[]};
    }
    else if(endpoint.startsWith('/api/catalog/definitions/'))payload=source.catalog_definition||{};
    else if(endpoint.endsWith('/draft-prompt')){requests.push({endpoint,body:JSON.parse(options.body)});payload={prompt:'Fixture prompt'};}
    else if(endpoint.endsWith('/draft-record'))payload=source.draft_record||{attempts:[]};
    else if(endpoint.endsWith('/draft') && options.method==='POST'){requests.push({endpoint,body:JSON.parse(options.body)});payload={job:{}};}
    else if(options.method==='PATCH'){const body=JSON.parse(options.body);requests.push({endpoint,body});const game=source.projects.find(p=>endpoint.includes(p.id));Object.assign(game,body);payload=game;}
    else if(endpoint==='/api/setup'){requests.push({endpoint,body:JSON.parse(options.body)});payload={project:source.projects[1],job:{}};}
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
  assert.equal(d.querySelectorAll('[data-activity-view]').length,9);assert.equal(activityNodes().length,8);
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
  [...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='Draft with Claude default').click();await tick();await tick();assert.equal(requests.at(-1).body.max_calls,1);
  d.getElementById('draft-corrections-'+id(2)).click();
  [...d.querySelectorAll('#project-content button')].find(n=>n.textContent==='Draft with Claude default').click();await tick();await tick();assert.equal(requests.at(-1).body.max_calls,3);
  d.getElementById('project-activity-tab').click();await tick();assert(d.querySelector('.job-panel .model-responses'),'Model responses are accessible beside the job logs too');dom.window.close();
  dom=await create(null,false,{...fixture,projects:[],artifacts:[]});assert(dom.window.document.getElementById('project-grid').textContent.includes('first game'));dom.window.close();
  console.log('UI layouts passed: add a game, next step, drafting settings, default workspace, saved preferences, collection/list switching, grouped platforms, RomM imports, correct counts, game selection, deep links, filters, empty state and blocked storage.');
})().catch(error=>{console.error(error);process.exitCode=1;});
