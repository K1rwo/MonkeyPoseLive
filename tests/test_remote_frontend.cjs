'use strict';
// No physical camera or model is opened: exercise transfer, cancellation and browser state.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {
  RemoteAPI,APIError,CameraRunner,uploadVideo,jobPath,containedRect,pointInFrame,
  validPoint,normalizeSample,segmentsForJoint,jpegBlob,mount
} = require('../web/remote.js');
const tests=[];
const test=(name,run)=>tests.push({name,run});
const settle=async()=>{for(let i=0;i<6;i++)await new Promise(resolve=>setImmediate(resolve));};
const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>data});
const defer=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};};

test('API sends same-origin cookies and CSRF only on mutations',async()=>{
  const calls=[],api=new RemoteAPI(async(url,options)=>{calls.push({url,options});return response({ok:true});});
  api.csrf='session-token';await api.request('/api/session');await api.json('/api/jobs','POST',{kind:'video'});
  assert.equal(calls[0].options.credentials,'same-origin');assert.equal(calls[0].options.cache,'no-store');
  assert.equal(calls[0].options.headers['X-CSRF-Token'],undefined);
  assert.equal(calls[1].options.headers['X-CSRF-Token'],'session-token');
  assert.equal(JSON.parse(calls[1].options.body).kind,'video');
});
test('API rejects other origins and never surfaces HTML proxy markup',async()=>{
  const api=new RemoteAPI(async()=>({ok:false,status:502,json:async()=>{throw new Error('not json');}}));
  await assert.rejects(()=>api.request('https://example.invalid/api/jobs'),/API 路径/);
  await assert.rejects(()=>api.request('/api/\\evil'),/API 路径/);
  await assert.rejects(()=>api.request('/api/jobs'),error=>error instanceof APIError&&error.status===502&&!error.message.includes('<'));
});
test('job IDs cannot become arbitrary URL paths',()=>{
  assert.equal(jobPath('abc_123-xyz','/result'),'/api/jobs/abc_123-xyz/result');
  for(const id of ['../config','a/b','a?x=1','a#fragment','',null])assert.throws(()=>jobPath(id));
});
test('JPEG responses decode without a fetch blocked by connect-src CSP',async()=>{
  const input=Uint8Array.from([255,216,255,224,0,16,255,217]);
  const blob=jpegBlob(Buffer.from(input).toString('base64'),value=>Buffer.from(value,'base64').toString('latin1'));
  assert.equal(blob.type,'image/jpeg');assert.deepEqual(new Uint8Array(await blob.arrayBuffer()),input);
  assert.throws(()=>jpegBlob('',()=>''),/画面无效/);
});
test('video upload sends sequential bounded chunks and final marker',async()=>{
  const file=new Blob([new Uint8Array(11)]),calls=[],progress=[];
  let active=0,maxActive=0;
  const api={request:async(url,options)=>{active++;maxActive=Math.max(active,maxActive);await settle();calls.push({url,options});active--;return{received:Number(new URL(url,'http://test').searchParams.get('offset'))+options.body.size};}};
  await uploadVideo(api,'job1',file,{chunkBytes:4,onProgress:(done,total)=>progress.push([done,total])});
  assert.equal(maxActive,1);assert.deepEqual(calls.map(call=>call.options.body.size),[4,4,3]);
  assert.deepEqual(calls.map(call=>call.url),['/api/jobs/job1/video?offset=0&final=0','/api/jobs/job1/video?offset=4&final=0','/api/jobs/job1/video?offset=8&final=1']);
  assert(calls.every(call=>call.options.headers['X-Upload-Length']==='11'));
  assert.deepEqual(progress,[[4,11],[8,11],[11,11]]);
});
test('upload rejects oversized, empty and inconsistent acknowledgements',async()=>{
  let calls=0;const api={request:async()=>{calls++;return{received:1};}};
  await assert.rejects(()=>uploadVideo(api,'job1',new Blob([])),/非空/);
  await assert.rejects(()=>uploadVideo(api,'job1',new Blob(['long']),{maxBytes:3}),/上限/);
  assert.equal(calls,0);
  await assert.rejects(()=>uploadVideo(api,'job1',new Blob(['long'])),/不一致/);
});
test('upload cancellation cannot send the next chunk',async()=>{
  const controller=new AbortController();let count=0;
  const api={request:async()=>{count++;return{};}};
  await assert.rejects(()=>uploadVideo(api,'job1',new Blob(['abcdefgh']),{chunkBytes:2,signal:controller.signal,onProgress:()=>controller.abort()}),error=>error.name==='AbortError');
  assert.equal(count,1);
});
test('camera waits for each response and aligns callback to submitted snapshot',async()=>{
  let captures=0,active=0,maxActive=0,stopped=0,received;
  const gate=defer(),snapshot={blob:new Blob(['frame']),width:640,height:480};
  const api={request:async(url,options)=>{assert(url.includes('sequence=1'));assert.strictEqual(options.body,snapshot.blob);active++;maxActive=Math.max(active,maxActive);await gate.promise;active--;return{sequence:1,points:[[1,2,.9]]};}};
  const runner=new CameraRunner(api,{clock:()=>100,onResult:(result,frame)=>{received=frame;runner.stop();}});
  const promise=runner.start({id:'camera1',stream:{getTracks:()=>[{stop:()=>stopped++}]},capture:async()=>{captures++;return snapshot;}});
  await settle();assert.equal(captures,1);assert.equal(active,1);gate.resolve();await promise;
  assert.equal(maxActive,1);assert.strictEqual(received,snapshot);assert.equal(stopped,1);
});
test('stopping a pending camera discards late results and releases tracks',async()=>{
  let displayed=0,stopped=0;const gate=defer();
  const runner=new CameraRunner({request:async()=>{await gate.promise;return{sequence:1};}},{onResult:()=>displayed++});
  const promise=runner.start({id:'camera2',stream:{getTracks:()=>[{stop:()=>stopped++}]},capture:async()=>({blob:new Blob(['frame'])})});
  await settle();runner.stop();gate.resolve();await promise;
  assert.equal(displayed,0);assert.equal(stopped,1);assert.equal(runner.running,false);
});
test('camera sequence mismatch stops rather than displaying another frame',async()=>{
  let displayed=0,failure,stopped=0;
  const runner=new CameraRunner({request:async()=>({sequence:8})},{onResult:()=>displayed++,onError:error=>{failure=error;}});
  await runner.start({id:'camera3',stream:{getTracks:()=>[{stop:()=>stopped++}]},capture:async()=>({blob:new Blob(['frame'])})});
  assert.equal(displayed,0);assert.match(failure.message,/序号不匹配/);assert.equal(stopped,1);
});
test('ROI coordinate conversion accounts for letterboxing',()=>{
  const rectangle=containedRect(800,450,640,480);
  assert.deepEqual(rectangle,{x:100,y:0,width:600,height:450});
  assert.equal(pointInFrame(20,225,rectangle),null);
  assert.deepEqual(pointInFrame(400,225,rectangle),[.5,.5]);
  assert.deepEqual(containedRect(0,450,640,480),{x:0,y:0,width:0,height:0});
});
test('object scores and native visibility masks preserve backend semantics',()=>{
  assert.equal(validPoint([10,20,.01],0,{score_semantics:'Object category score'},.8),true);
  assert.equal(validPoint([10,20,.99],0,{display_valid:[false]},.2),false);
  assert.equal(validPoint([10,20,.01],0,{display_valid:[true]},.8),true);
  assert.equal(validPoint([10,NaN,.9],0,{},.2),false);
  assert.equal(validPoint([10,20,.9],0,{display_indices:[1]},.2),false);
});
test('trajectory gaps are not interpolated and axes use actual frame dimensions',()=>{
  const samples=[
    {time:0,keypoints_xy_score:[[20,10,.9]],valid:[true],frame_width:100,frame_height:50},
    {time:.5,keypoints_xy_score:[[40,20,.9]],valid:[true],frame_width:100,frame_height:50},
    {time:.7,keypoints_xy_score:[[50,30,.9]],valid:[false],frame_width:100,frame_height:50},
    {time:.9,keypoints_xy_score:[[60,35,.9]],valid:[true],frame_width:100,frame_height:50},
    {time:3,keypoints_xy_score:[[70,40,.9]],valid:[true],frame_width:100,frame_height:50}
  ].map(sample=>normalizeSample(sample));
  const segments=segmentsForJoint(samples,0);
  assert.equal(segments.length,1);assert.equal(segments[0][1].x,.4);assert.equal(segments[0][1].y,.4);
  assert.equal(normalizeSample({time:0,points:[]}),null);
});

function fakeDOM({secure=true,initialJobs=[]}={}){
  const html=fs.readFileSync(path.join(__dirname,'../web/remote.html'),'utf8'),ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]);
  assert.equal(new Set(ids).size,ids.length,'IDs must be unique');
  const element=(id='')=>{
    const classes=new Set(),listeners={};
    return{id,classes,listeners,value:id==='threshold'?'0.2':id==='joint'?'0':'',disabled:false,checked:false,dataset:{},files:[],options:[],width:640,height:480,videoWidth:640,videoHeight:480,readyState:2,
      classList:{add:name=>classes.add(name),remove:name=>classes.delete(name),toggle(name,on){if(on===undefined)on=!classes.has(name);on?classes.add(name):classes.delete(name);},contains:name=>classes.has(name)},
      setAttribute(name,value){this[name]=value;},removeAttribute(name){delete this[name];},addEventListener(name,fn){listeners[name]=fn;},
      replaceChildren(){this.options=[];this.value='';},append(node){this.options.push(node);if(this.options.length===1)this.value=node.value;},
      getBoundingClientRect(){return{width:800,height:id==='chartCanvas'?170:450,left:0,top:0};},
      getContext(){return new Proxy({},{get:()=>()=>{}});},toBlob(callback){callback(new Blob(['jpeg'],{type:'image/jpeg'}));},
      play:async()=>{},pause(){},load(){},setPointerCapture(){},hasPointerCapture(){return false;},releasePointerCapture(){}};
  };
  const nodes=Object.fromEntries(ids.map(id=>[id,element(id)])),document={getElementById:id=>nodes[id],createElement:tag=>element(tag)};
  let serverStatus='queued',stopped=0,frameCalls=0;const requests=[],timers=new Map(),gate=defer();let timerID=0;
  const browser={document,isSecureContext:secure,devicePixelRatio:1,navigator:{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[{stop:()=>stopped++}]})}},URL:{createObjectURL:()=>'/blob/preview',revokeObjectURL:()=>{}},addEventListener(){},clearTimeout:id=>timers.delete(id),setTimeout:fn=>{const id=++timerID;timers.set(id,fn);return id;},
    fetch:async(url,options={})=>{
      requests.push({url,options});
      if(url==='/api/session')return response({authenticated:true,csrf:'csrf-test',models:[{id:'mmpose',label:'Test pose',ready:true,detector_supported:true}],limits:{max_upload_bytes:500,chunk_bytes:4}});
      if(url==='/api/jobs'&&(!options.method||options.method==='GET'))return response({jobs:initialJobs});
      if(url==='/api/jobs'&&options.method==='POST')return response({id:'job123',kind:JSON.parse(options.body).kind,status:'queued'});
      if(url==='/api/jobs/job123'&&options.method==='DELETE')return response({id:'job123',status:'cancelled',processed:0});
      if(url==='/api/jobs/job123')return response({id:'job123',kind:'camera',status:serverStatus,processed:0});
      if(url.includes('/frame?')){frameCalls++;await gate.promise;return response({sequence:1,points:[[10,10,.9]],width:640,height:480,keypoint_names:['nose']});}
      if(url==='/api/logout')return response({ok:true});
      return response({});
    }};
  return{nodes,document,browser,requests,timers,gate,setStatus:value=>{serverStatus=value;},stopped:()=>stopped,frameCalls:()=>frameCalls};
}
test('insecure remote HTTP disables browser camera while video stays available',async()=>{
  const fake=fakeDOM({secure:false}),app=mount(fake.document,fake.browser);await settle();
  await fake.nodes.cameraTab.listeners.click();await settle();
  assert.equal(app.state.kind,'camera');assert.equal(fake.nodes.startButton.disabled,true);assert.equal(fake.nodes.cameraPreviewButton.disabled,true);
  assert.equal(fake.nodes.cameraWarning.classList.contains('hidden'),false);
  assert.equal(fake.frameCalls(),0);
});
test('camera waits for backend readiness and stop releases local hardware',async()=>{
  const fake=fakeDOM(),app=mount(fake.document,fake.browser);await settle();
  await fake.nodes.cameraTab.listeners.click();await fake.nodes.cameraPreviewButton.listeners.click();await settle();
  assert(app.state.stream);await app.startJob();await settle();
  assert.equal(fake.frameCalls(),0,'queued job must not submit a frame');
  fake.setStatus('running');
  const timer=[...fake.timers.values()][0];assert(timer);fake.timers.clear();await timer();await settle();
  assert.equal(fake.frameCalls(),1);
  await app.stopJob();fake.gate.resolve();await settle();
  assert(fake.stopped()>0);assert.equal(app.state.stream,null);assert.equal(app.state.job.status,'cancelled');
});
test('logout hides private preview and releases browser camera',async()=>{
  const fake=fakeDOM(),app=mount(fake.document,fake.browser);await settle();
  await fake.nodes.cameraTab.listeners.click();await fake.nodes.cameraPreviewButton.listeners.click();await settle();
  assert(app.state.frame);await fake.nodes.logoutButton.listeners.click();await settle();
  assert.equal(app.state.authenticated,false);assert.equal(app.state.frame,null);assert.equal(app.state.stream,null);
  assert(fake.stopped()>0);assert.equal(fake.nodes.application.classList.contains('hidden'),true);assert.equal(app.api.csrf,'');
});
test('reload resumes a session-owned queued video without another upload',async()=>{
  const fake=fakeDOM({initialJobs:[{id:'job123',kind:'video',model:'mmpose',status:'queued'}]}),app=mount(fake.document,fake.browser);await settle();
  assert.equal(app.state.job.id,'job123');
  assert(fake.requests.some(request=>request.url==='/api/jobs/job123'&&request.options.method==='GET'));
  assert.equal(fake.requests.filter(request=>request.options.method==='PUT').length,0);
  assert.equal(fake.frameCalls(),0);
});
test('reload cancels interrupted uploads and requests a new File selection',async()=>{
  const fake=fakeDOM({initialJobs:[{id:'job123',kind:'video',status:'uploading'}]}),app=mount(fake.document,fake.browser);await settle();
  assert.equal(app.state.job,null);
  assert(fake.requests.some(request=>request.url==='/api/jobs/job123'&&request.options.method==='DELETE'));
  assert.match(fake.nodes.message.textContent,/重新选择视频/);
});
test('reload cancels old camera job without opening any local camera',async()=>{
  const fake=fakeDOM({initialJobs:[{id:'job123',kind:'camera',status:'running'}]}),app=mount(fake.document,fake.browser);await settle();
  assert.equal(app.state.job.status,'cancelled');assert.equal(app.state.stream,null);assert.equal(fake.frameCalls(),0);
  assert(fake.requests.some(request=>request.url==='/api/jobs/job123'&&request.options.method==='DELETE'));
  assert.match(fake.nodes.message.textContent,/重新预览摄像头/);
});

(async()=>{
  let failed=0;
  for(const {name,run}of tests){try{await run();process.stdout.write(`PASS ${name}\n`);}catch(error){failed++;process.stderr.write(`FAIL ${name}\n${error.stack}\n`);}}
  process.stdout.write(`${tests.length-failed}/${tests.length} remote frontend tests passed\n`);
  process.exitCode=failed?1:0;
})();
