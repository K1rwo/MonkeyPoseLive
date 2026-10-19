'use strict';
// Pure fake-DOM regression: no browser, HTTP server, model or camera is opened.
// node tests/test_frontend.cjs [path/to/index.html] [--report-dir DIR]
const fs=require('fs'),path=require('path'),vm=require('vm'),assert=require('assert'),crypto=require('crypto');
const args=process.argv.slice(2),htmlArg=args[0]&&!args[0].startsWith('--')?args.shift():null;
const reportIndex=args.indexOf('--report-dir'),reportDirectory=reportIndex>=0?path.resolve(args[reportIndex+1]):null;
const htmlPath=htmlArg?path.resolve(htmlArg):path.resolve(__dirname,'../web/index.html');
const html=fs.readFileSync(htmlPath,'utf8');
const code=[...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)].map(match=>match[1]).join('\n');
new Function(code);
const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]);
assert.strictEqual(new Set(ids).size,ids.length);
const tests=[],clone=value=>JSON.parse(JSON.stringify(value));
const defaults={model:'mmpose',device:'cuda',backend:'auto',cameraIndex:'0',threshold:'0.2',trailSeconds:'3'};
function element(id='') {
  const classes=new Set();
  return {id,dataset:{},value:defaults[id]||'',checked:id==='detector',disabled:false,options:[],listeners:{},attributes:{},
    classList:{toggle(name,on){on?classes.add(name):classes.delete(name)},add(name){classes.add(name)},remove(name){classes.delete(name)},contains(name){return classes.has(name)}},
    setAttribute(name,value){this.attributes[name]=value},removeAttribute(name){delete this.attributes[name]},showModal(){this.open=true},close(){this.open=false},addEventListener(name,callback){this.listeners[name]=callback},
    getBoundingClientRect(){return {left:0,top:0,width:800,height:id==='trajectoryChart'?200:450}},
    getContext(){return new Proxy({},{get:()=>()=>{}})},replaceChildren(){this.options=[];this.value=''},
    append(...items){this.options.push(...items);if(this.id==='jointSelect'&&!this.value&&items.length)this.value=items[0].value},
    checkValidity(){return true},focus(){},setPointerCapture(){},hasPointerCapture(){return true},releasePointerCapture(){}};
}
const nodes=Object.fromEntries(ids.map(id=>[id,element(id)]));
const names=['nose','left_eye','right_eye','left_ear','right_ear','left_shoulder','right_shoulder','left_elbow','right_elbow','left_wrist','right_wrist','left_hip','right_hip','left_knee','right_knee','left_ankle','right_ankle'];
let remote={status:'running',run_directory:'synthetic_run_A',model:'macaque3dpose_2d',source_type:'file',frame_width:640,frame_height:480,keypoint_names:names,display_indices:[0,3,4,5,6,7,8,9,10,11,12,13,14,15,16],configuration_epoch:10,
  configuration:{source_type:'file',source:'/fixtures/clip.mp4',model:'macaque3dpose_2d',device:'auto',backend:'msmf',detector:true,roi:[.2,.1,.8,.9],threshold:.6,trail_seconds:20,record:false}};
let trajectory={epoch:10,keypoint_names:names,max_gap_seconds:1,samples:[{time:10,keypoints_xy_score:[[100,100,.9]],valid:[true],status:'ok',frame_width:640,frame_height:480}]};
let clock=0,timerID=0,pollCallback,timers=new Map(),activeConfig=0,maxActiveConfig=0,pendingConfig=[],holdNextState=false,heldState=null;
const requests=[];
const modelIDs=['mmpose','macaque3dpose_2d','vitpose_animal','xpose','dlc'];
const readiness=ready=>Object.fromEntries(modelIDs.map(id=>[id,{id,label:id,ready,missing:ready?[]:['python','checkpoint'],detector_supported:id!=='dlc'}]));
let setupServer={config_path:'/fixtures/monkeypose-live.json',output_directory:'/fixtures/output',detector_ready:true,configuration_error:null,models:readiness(true),
  configuration:{schema_version:1,base_python:'/fixtures/base-python',output_directory:'/fixtures/output',detector:{checkpoint:'/fixtures/detector.pt',device:'cpu'},models:Object.fromEntries(modelIDs.map(id=>[id,{python:'/fixtures/python',repo:'/fixtures/source',config:'/fixtures/config.py',checkpoint:'/fixtures/checkpoint',custom_option:'preserve-me'}]))}};
const settle=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve))};
function timeout(callback,delay){const id=++timerID;timers.set(id,{callback,due:clock+delay});return id}
async function advance(ms){clock+=ms;for(let i=0;i<10;i++){const due=[...timers].filter(([,timer])=>timer.due<=clock).sort((a,b)=>a[1].due-b[1].due);if(!due.length)break;for(const [id,timer]of due){timers.delete(id);timer.callback()}await settle()}}
function response(data){return {ok:true,status:200,json:async()=>clone(data)}}
async function fetchMock(url,options={}) {
  assert(url.startsWith('/'),'Only relative local API paths are allowed');
  if(url==='/api/state' && holdNextState){holdNextState=false;const captured=clone(remote);return await new Promise(resolve=>{heldState=()=>resolve(response(captured))});}
  if(options.method==='POST') {
    const body=JSON.parse(options.body);requests.push({url,body:clone(body)});
    if(url==='/api/setup'){
      if(['running','loading_model','stopping_capture'].includes(remote.status))return {ok:false,status:409,json:async()=>({error:'Stop the active session first'})};
      setupServer.configuration=clone(body);setupServer.output_directory=body.output_directory||'/fixtures/default-output';setupServer.configuration_error=null;
      setupServer.detector_ready=Boolean(body.detector?.checkpoint);
      setupServer.models=Object.fromEntries(modelIDs.map(id=>{const entry=body.models?.[id]||{},ready=Boolean((entry.python||body.base_python)&&entry.checkpoint);return [id,{id,label:id,ready,missing:ready?[]:['python','checkpoint'],detector_supported:id!=='dlc'}]}));
      return response(setupServer);
    }
    if(url==='/api/config') {
      activeConfig++;maxActiveConfig=Math.max(maxActiveConfig,activeConfig);
      return await new Promise(resolve=>pendingConfig.push({body,finish(){
        const before=remote.configuration;
        const changed=JSON.stringify(before.roi)!==JSON.stringify(body.roi)||before.threshold!==body.threshold||before.detector!==body.detector;
        remote.configuration={...before,...clone(body)};if(changed)remote.configuration_epoch++;
        activeConfig--;resolve(response(remote));
      }}));
    }
    if(url==='/api/stop')remote.status='stopped';
    if(url==='/api/start')remote={...remote,status:'loading_model',configuration:clone(body),model:body.model};
  }
  return response(url==='/api/setup'?setupServer:url==='/api/models'?setupServer.models:url==='/api/trajectory'?trajectory:remote);
}
const context={console,document:{getElementById:id=>nodes[id],createElement:()=>element()},window:{devicePixelRatio:1,addEventListener(){}},fetch:fetchMock,AbortController,
  setTimeout:timeout,clearTimeout:id=>timers.delete(id),setInterval:callback=>{pollCallback=callback;return 1}};
const instrumented=code.replace(/\}\)\(\);\s*$/,'globalThis.frontendTestState=()=>({roi,sourceType,samples:samples.map(s=>s.time),configurationEpoch,configurationDirty,configInFlight,setupDraft,setupInputs,setupMode,modelRegistry});})();');
async function poll(){await pollCallback();await settle()}
async function finishConfig(){assert(pendingConfig.length);pendingConfig.shift().finish();await settle()}
function pass(name){tests.push({name,passed:true})}
async function main(){
  vm.runInNewContext(instrumented,context);await settle();
  assert.strictEqual(nodes.fileTab.attributes['aria-selected'],'true');
  assert.strictEqual(nodes.videoPath.value,'/fixtures/clip.mp4');
  assert.strictEqual(nodes.model.value,'macaque3dpose_2d');assert.strictEqual(nodes.device.value,'auto');assert.strictEqual(nodes.backend.value,'msmf');
  assert.strictEqual(nodes.threshold.value,'0.6');assert.strictEqual(nodes.trailSeconds.value,'20');assert.strictEqual(nodes.record.checked,false);
  assert.deepStrictEqual(clone(context.frontendTestState().roi),[.2,.1,.8,.9]);assert.strictEqual(nodes.jointSelect.options.length,15);
  assert(/id="trailSeconds"[^>]*max="30"/.test(html));assert(html.includes('<option value="auto">自动选择</option>'));
  pass('refresh_running_file_restores_all_configuration_fields_including_ROI_auto_and_long_trail');

  nodes.threshold.value='.75';nodes.threshold.listeners.input();await poll();
  assert.strictEqual(nodes.threshold.value,'.75');assert.deepStrictEqual(clone(context.frontendTestState().roi),[.2,.1,.8,.9]);
  await advance(180);assert.strictEqual(pendingConfig.length,1);assert.strictEqual(pendingConfig[0].body.threshold,.75);
  assert.deepStrictEqual(pendingConfig[0].body.roi,[.2,.1,.8,.9]);pass('slider_after_refresh_posts_restored_ROI_without_resetting_full_frame');

  remote.configuration_epoch=11;remote.configuration={...remote.configuration,threshold:.3,roi:[.1,.1,.6,.6]};
  trajectory={...trajectory,epoch:11,samples:[{...trajectory.samples[0],time:11}]};await poll();
  assert.strictEqual(nodes.threshold.value,'.75');assert.deepStrictEqual(clone(context.frontendTestState().roi),[.2,.1,.8,.9]);
  assert.deepStrictEqual(clone(context.frontendTestState().samples),[11]);pass('inflight_local_edit_not_overwritten_by_external_poll_and_trajectory_epoch_still_clears');

  nodes.threshold.value='.82';nodes.threshold.listeners.input();await advance(180);
  assert.strictEqual(pendingConfig.length,1);await finishConfig();assert.strictEqual(nodes.threshold.value,'.82');
  await advance(180);assert.strictEqual(pendingConfig.length,1);assert.strictEqual(pendingConfig[0].body.threshold,.82);await finishConfig();
  assert.strictEqual(maxActiveConfig,1);assert.strictEqual(context.frontendTestState().configurationDirty,false);
  const ackEpoch=remote.configuration_epoch;pass('overlapping_local_edits_are_serialized_and_latest_revision_wins');

  const current=clone(remote);remote={...remote,configuration_epoch:11,configuration:{...remote.configuration,threshold:.1,roi:[0,0,1,1]}};
  trajectory={...trajectory,epoch:ackEpoch,samples:[{...trajectory.samples[0],time:13}]};await poll();
  assert.strictEqual(nodes.threshold.value,'.82');assert.deepStrictEqual(clone(context.frontendTestState().roi),[.2,.1,.8,.9]);
  assert.deepStrictEqual(clone(context.frontendTestState().samples),[13]);pass('configuration_ACK_epoch_prevents_old_poll_rollback_without_regressing_trajectory_isolation');

  remote={...current,configuration_epoch:ackEpoch+1,configuration:{...current.configuration,roi:[.25,.2,.7,.8],threshold:.4,detector:false}};
  trajectory={...trajectory,epoch:remote.configuration_epoch,samples:[{...trajectory.samples[0],time:14}]};await poll();
  assert.strictEqual(nodes.threshold.value,'0.4');assert.strictEqual(nodes.detector.checked,false);assert.deepStrictEqual(clone(context.frontendTestState().roi),[.25,.2,.7,.8]);
  pass('external_active_configuration_epoch_synchronizes_when_no_local_edit_is_pending');

  remote.status='stopped';await poll();nodes.usbTab.listeners.click();nodes.cameraIndex.value='1';nodes.model.value='vitpose_animal';nodes.model.listeners.change();nodes.device.value='cpu';nodes.record.checked=true;nodes.threshold.value='.91';nodes.threshold.listeners.input();
  remote.configuration_epoch++;remote.configuration.threshold=.1;await poll();
  assert.strictEqual(nodes.usbTab.attributes['aria-selected'],'true');assert.strictEqual(nodes.cameraIndex.value,'1');assert.strictEqual(nodes.model.value,'vitpose_animal');assert.strictEqual(nodes.device.value,'cpu');assert.strictEqual(nodes.record.checked,true);assert.strictEqual(nodes.threshold.value,'.91');
  pass('stopped_poll_does_not_overwrite_next_session_user_choices');

  remote={...remote,status:'running',run_directory:'synthetic_run_B',model:'mmpose',source_type:'usb',configuration_epoch:30,display_indices:names.map((_,i)=>i),configuration:{source_type:'usb',source:'2',model:'mmpose',device:'cuda',backend:'auto',detector:true,roi:[0,0,1,1],threshold:.2,trail_seconds:3,record:false}};
  trajectory={...trajectory,epoch:30,samples:[{...trajectory.samples[0],time:30}]};await poll();
  assert.strictEqual(nodes.cameraIndex.value,'2');assert.strictEqual(nodes.model.value,'mmpose');assert.strictEqual(nodes.threshold.value,'0.2');assert.strictEqual(nodes.record.checked,false);assert.strictEqual(context.frontendTestState().roi,null);
  pass('new_run_directory_restores_authoritative_USB_configuration_and_clears_prior_history');

  nodes.roiCanvas.listeners.pointerdown({button:0,clientX:220,clientY:90,pointerId:1,preventDefault(){}});
  remote.configuration_epoch=31;remote.configuration.roi=[.05,.05,.9,.9];await poll();
  assert.strictEqual(context.frontendTestState().roi,null);
  nodes.roiCanvas.listeners.pointermove({clientX:580,clientY:360});
  nodes.roiCanvas.listeners.pointerup({clientX:580,clientY:360,pointerId:1});await poll();
  const dragged=clone(context.frontendTestState().roi);assert.deepStrictEqual(dragged,[.2,.2,.8,.8]);
  await advance(180);assert.deepStrictEqual(pendingConfig[0].body.roi,dragged);await finishConfig();
  pass('ROI_drag_and_pending_send_are_not_stolen_by_configuration_poll');

  await nodes.stopButton.listeners.click();
  holdNextState=true;const oldPoll=pollCallback();await settle();assert(heldState);
  nodes.fileTab.listeners.click();nodes.videoPath.value='/fixtures/new-clip.mp4';nodes.model.value='vitpose_animal';nodes.model.listeners.change();
  await nodes.startButton.listeners.click();assert.strictEqual(nodes.statusText.textContent,'模型载入中');
  heldState();await oldPoll;await settle();
  assert.strictEqual(nodes.statusText.textContent,'模型载入中');assert.strictEqual(nodes.model.value,'vitpose_animal');assert.strictEqual(nodes.startButton.disabled,true);
  pass('pre_start_state_poll_cannot_roll_back_new_start_response_or_unlock_running_form');

  remote.status='stopped';await poll();setupServer.models=readiness(false);setupServer.detector_ready=false;
  setupServer.configuration={schema_version:1,base_python:'',output_directory:'',detector:{checkpoint:'',device:'cpu'},models:{mmpose:{custom_option:'preserve-me'}}};
  await nodes.openSetup.listeners.click();await settle();
  const startRequests=requests.filter(item=>item.url==='/api/start').length;
  await nodes.startButton.listeners.click();
  assert.strictEqual(requests.filter(item=>item.url==='/api/start').length,startRequests);assert.strictEqual(nodes.setupDialog.open,true);
  assert.strictEqual(nodes.detector.checked,false);assert.strictEqual(nodes.detector.disabled,true);assert(!nodes.setupBanner.classList.contains('hidden'));
  pass('first_run_missing_models_shows_setup_and_never_starts_model_or_camera');

  nodes.setupModel.value='mmpose';nodes.setupModel.listeners.change();
  const fields=context.frontendTestState().setupInputs;fields.python.value='/fixtures/new-python';fields.config.value='/fixtures/new-model.py';fields.checkpoint.value='/fixtures/new-weights.pth';
  nodes.setupOutput.value='/fixtures/new-output';nodes.setupBasePython.value='/fixtures/base-python';nodes.setupDetector.value='/fixtures/detector.pt';
  await nodes.saveSetup.listeners.click();
  assert.strictEqual(setupServer.configuration.models.mmpose.python,'/fixtures/new-python');assert.strictEqual(setupServer.configuration.models.mmpose.custom_option,'preserve-me');assert.strictEqual(setupServer.configuration.output_directory,'/fixtures/new-output');assert.strictEqual(setupServer.models.mmpose.ready,true);
  assert.strictEqual(requests.filter(item=>item.url==='/api/start').length,startRequests);
  pass('basic_path_form_saves_schema_preserves_advanced_fields_and_only_checks_readiness');

  nodes.setupJsonTab.listeners.click();nodes.setupJson.value='{ invalid json';
  const saves=requests.filter(item=>item.url==='/api/setup').length;await nodes.saveSetup.listeners.click();
  assert.strictEqual(requests.filter(item=>item.url==='/api/setup').length,saves);assert(nodes.setupMessage.classList.contains('error'));
  const advanced=clone(setupServer.configuration);advanced.models.mmpose.checkpoint_sha256='synthetic-test-hash';nodes.setupJson.value=JSON.stringify(advanced);nodes.setupBasicTab.listeners.click();
  await nodes.saveSetup.listeners.click();assert.strictEqual(setupServer.configuration.models.mmpose.checkpoint_sha256,'synthetic-test-hash');
  pass('advanced_JSON_validation_and_form_roundtrip_preserve_hash_fields');

  setupServer.configuration_error='Synthetic invalid configuration JSON';await nodes.refreshSetup.listeners.click();
  assert(nodes.setupMessage.textContent.includes('Synthetic invalid configuration JSON'));assert.strictEqual(nodes.saveSetup.disabled,false);
  await nodes.saveSetup.listeners.click();assert.strictEqual(setupServer.configuration_error,null);
  pass('damaged_configuration_can_be_repaired_without_blank_UI_or_automatic_download');

  remote={...remote,status:'running',model:'mmpose',configuration:{...remote.configuration,model:'mmpose'}};await poll();await nodes.openSetup.listeners.click();
  assert.strictEqual(nodes.setupOutput.disabled,true);assert.strictEqual(nodes.setupJson.readOnly,true);assert.strictEqual(nodes.saveSetup.disabled,true);
  const runningSaves=requests.filter(item=>item.url==='/api/setup').length;await nodes.saveSetup.listeners.click();
  assert.strictEqual(requests.filter(item=>item.url==='/api/setup').length,runningSaves);
  pass('active_capture_disables_backend_path_edits_and_blocks_setup_POST');
}
main().then(()=>{
  const report={all_passed:true,created_utc:new Date().toISOString(),html:htmlPath,html_sha256:crypto.createHash('sha256').update(html).digest('hex'),
    execution:'Node fake DOM and synthetic HTTP responses only',physical_camera_opened:false,model_loaded:false,server_started:false,network_requests_made:false,
    test_count:tests.length,tests,max_concurrent_configuration_requests:maxActiveConfig};
  let output=null;if(reportDirectory){fs.mkdirSync(reportDirectory,{recursive:true});output=path.join(reportDirectory,'regression_report_'+Date.now()+'.json');fs.writeFileSync(output,JSON.stringify(report,null,2)+'\n',{flag:'wx'});}
  console.log(JSON.stringify({...report,report:output}));
}).catch(error=>{
  if(reportDirectory){fs.mkdirSync(reportDirectory,{recursive:true});const output=path.join(reportDirectory,'regression_failure_'+Date.now()+'.json');fs.writeFileSync(output,JSON.stringify({all_passed:false,tests,error:String(error.stack)},null,2)+'\n',{flag:'wx'});console.error(output);}
  console.error(error);process.exitCode=1;
});
