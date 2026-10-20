/* MonkeyPoseLive network client. No inference library or private configuration is sent to clients. */
(function (root, factory) {
  'use strict';
  const exported = factory();
  if (typeof module === 'object' && module.exports) module.exports = exported;
  if (root && root.document) {
    root.MonkeyPoseRemote = exported;
    const initialize = () => exported.mount(root.document, root);
    if (root.document.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', initialize, {once: true});
    else initialize();
  }
})(typeof window === 'undefined' ? null : window, function () {
  'use strict';
  const TERMINAL = new Set(['completed', 'done', 'failed', 'error', 'cancelled', 'canceled', 'stopped']);
  const STATUS_LABELS = {created: '待上传', uploading: '上传中', queued: '排队中', loading: '载入模型', loading_model: '载入模型', starting: '启动中', running: '分析中', processing: '分析中', completed: '已完成', done: '已完成', failed: '分析失败', error: '分析失败', cancelling: '停止中', stopping: '停止中', cancelled: '已停止', canceled: '已停止', stopped: '已停止'};
  const DEFAULT_CHUNK = 4 * 1024 * 1024;
  const DEFAULT_MAX = 512 * 1024 * 1024;
  const clamp = (number, low, high) => Math.max(low, Math.min(high, number));
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const jobObject = payload => payload && (payload.job || payload);
  const aborted = () => { const error = new Error('操作已停止'); error.name = 'AbortError'; return error; };

  class APIError extends Error {
    constructor(message, status) { super(message); this.name = 'APIError'; this.status = status; }
  }
  class RemoteAPI {
    constructor(fetchFunction) { this.fetch = fetchFunction; this.csrf = ''; }
    async request(path, options = {}) {
      if (!path.startsWith('/api/') || path.startsWith('//') || path.includes('\\')) throw new Error('无效的 API 路径');
      const method = (options.method || 'GET').toUpperCase();
      const headers = {...options.headers};
      if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && this.csrf) headers['X-CSRF-Token'] = this.csrf;
      const response = await this.fetch(path, {...options, method, headers, credentials: 'same-origin', cache: 'no-store'});
      let data = null;
      try { data = await response.json(); } catch (_) { /* Never display an HTML proxy error as markup. */ }
      if (!response.ok) throw new APIError(data && typeof data.error === 'string' ? data.error : `服务请求失败（${response.status}）`, response.status);
      return data || {};
    }
    json(path, method, body, signal) {
      return this.request(path, {method, body: JSON.stringify(body), headers: {'Content-Type': 'application/json'}, signal});
    }
    async login(password) {
      const result = await this.json('/api/login', 'POST', {password});
      if (result.csrf) this.csrf = result.csrf;
      return result;
    }
  }
  function jobPath(id, suffix = '') {
    if (typeof id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(id)) throw new Error('服务返回了无效的任务编号');
    return `/api/jobs/${id}${suffix}`;
  }
  async function uploadVideo(api, id, file, options = {}) {
    const maxBytes = Number(options.maxBytes) || DEFAULT_MAX;
    const chunkBytes = Math.min(DEFAULT_CHUNK, Math.max(1, Number(options.chunkBytes) || DEFAULT_CHUNK));
    if (!file || !Number.isSafeInteger(file.size) || file.size <= 0) throw new Error('请选择非空视频文件');
    if (file.size > maxBytes) throw new Error(`视频超过 ${formatBytes(maxBytes)} 的上传上限`);
    let received = 0, result = {};
    while (received < file.size) {
      if (options.signal && options.signal.aborted) throw aborted();
      const end = Math.min(file.size, received + chunkBytes);
      result = await api.request(jobPath(id, `/video?offset=${received}&final=${end === file.size ? 1 : 0}`), {
        method: 'PUT', body: file.slice(received, end), signal: options.signal,
        headers: {'Content-Type': 'application/octet-stream', 'X-Upload-Length': String(file.size)}
      });
      const acknowledged = result.received ?? result.bytes_received;
      if (acknowledged !== undefined && Number(acknowledged) !== end) throw new Error('上传进度与服务器不一致，请重新创建任务');
      received = end;
      if (options.onProgress) options.onProgress(received, file.size);
    }
    return result;
  }
  class CameraRunner {
    constructor(api, options = {}) {
      this.api = api; this.onResult = options.onResult || (() => {}); this.onError = options.onError || (() => {});
      this.pause = options.pause || (milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds)));
      this.clock = options.clock || (() => performance.now());
      this.epoch = 0; this.running = false; this.stream = null; this.controller = null; this.promise = null;
    }
    start({id, stream, capture}) {
      this.stop();
      const epoch = ++this.epoch;
      this.stream = stream; this.running = true;
      const controller = new AbortController(); this.controller = controller;
      const began = this.clock();
      this.promise = (async () => {
        let sequence = 0;
        while (this.running && epoch === this.epoch) {
          const snapshot = await capture();
          if (!this.running || epoch !== this.epoch) return;
          const sent = this.clock();
          const result = await this.api.request(jobPath(id, `/frame?sequence=${++sequence}&timestamp=${Math.max(0, (sent - began) / 1000).toFixed(6)}`), {
            method: 'POST', headers: {'Content-Type': 'image/jpeg'}, body: snapshot.blob, signal: controller.signal
          });
          if (!this.running || epoch !== this.epoch) return;
          if (result.sequence !== undefined && Number(result.sequence) !== sequence) throw new Error('服务返回的画面序号不匹配，分析已停止');
          await this.onResult(result, snapshot, this.clock() - sent);
          if (!this.running || epoch !== this.epoch) return;
          await this.pause(80); // At most one frame in flight; never build a video backlog.
        }
      })().catch(error => {
        if (epoch !== this.epoch || error.name === 'AbortError') return;
        this.stop(); this.onError(error);
      });
      return this.promise;
    }
    stop() {
      this.epoch++; this.running = false;
      if (this.controller) this.controller.abort();
      this.controller = null;
      if (this.stream) for (const track of this.stream.getTracks()) track.stop();
      this.stream = null;
    }
  }
  function formatBytes(bytes) {
    if (!finite(Number(bytes))) return '—';
    if (bytes >= 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GiB`;
    if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
    return `${Math.ceil(bytes / 1024)} KiB`;
  }
  function jpegBlob(encoded, decode) {
    if(typeof encoded!=='string'||!encoded||encoded.length>16*1024*1024)throw new Error('服务返回的分析画面无效');
    const binary=decode(encoded),bytes=new Uint8Array(binary.length);
    for(let index=0;index<binary.length;index++)bytes[index]=binary.charCodeAt(index);
    return new Blob([bytes],{type:'image/jpeg'});
  }
  function containedRect(width, height, imageWidth, imageHeight) {
    if (![width, height, imageWidth, imageHeight].every(value => finite(value) && value > 0)) return {x: 0, y: 0, width: 0, height: 0};
    const scale = Math.min(width / imageWidth, height / imageHeight);
    const fittedWidth = imageWidth * scale, fittedHeight = imageHeight * scale;
    return {x: (width - fittedWidth) / 2, y: (height - fittedHeight) / 2, width: fittedWidth, height: fittedHeight};
  }
  function pointInFrame(x, y, rectangle) {
    if (!rectangle.width || !rectangle.height || x < rectangle.x || x > rectangle.x + rectangle.width || y < rectangle.y || y > rectangle.y + rectangle.height) return null;
    return [clamp((x - rectangle.x) / rectangle.width, 0, 1), clamp((y - rectangle.y) / rectangle.height, 0, 1)];
  }
  function validPoint(point, index, result, threshold) {
    if (!Array.isArray(point) || point.length < 3 || !point.every(finite)) return false;
    const validity = result.display_valid || result.valid;
    if (Array.isArray(validity) && !validity[index]) return false;
    if (result.status && !['ok', 'running', 'processing'].includes(result.status)) return false;
    if (Array.isArray(result.display_indices) && !result.display_indices.includes(index)) return false;
    if (String(result.score_semantics || '').toLowerCase().includes('object')) return true;
    return Array.isArray(validity) ? Boolean(validity[index]) : point[2] >= threshold;
  }
  function normalizeSample(sample, fallback = {}) {
    const points = sample.points || sample.keypoints_xy_score || [];
    const width = Number(sample.width || sample.frame_width || fallback.width || fallback.frame_width);
    const height = Number(sample.height || sample.frame_height || fallback.height || fallback.frame_height);
    const time = Number(sample.timestamp ?? sample.time ?? fallback.timestamp);
    if (!finite(time) || !(width > 0) || !(height > 0)) return null;
    return {...fallback, ...sample, time, points, width, height, keypoints_xy_score: points};
  }
  function segmentsForJoint(samples, joint, threshold = .2, seconds = 15) {
    const output = [], end = samples.length ? samples[samples.length - 1].time : 0;
    let previous = null;
    for (const sample of samples) {
      if (sample.time < end - seconds) continue;
      const point = sample.keypoints_xy_score[joint];
      const valid = validPoint(point, joint, sample, threshold) && point[0] >= 0 && point[0] < sample.width && point[1] >= 0 && point[1] < sample.height;
      const normalized = valid ? {time: sample.time, x: point[0] / sample.width, y: point[1] / sample.height} : null;
      if (previous && normalized && normalized.time > previous.time && normalized.time - previous.time <= 1) output.push([previous, normalized]);
      previous = normalized;
    }
    return output;
  }

  function mount(document, window) {
    const byId = id => document.getElementById(id);
    const elements = Object.fromEntries(['loginPanel','loginForm','password','loginButton','loginMessage','application','logoutButton','connection','connectionText','model','modelNote','detector','detectorNote','videoTab','cameraTab','videoFields','cameraFields','videoFile','fileName','fileLimit','cameraPreviewButton','cameraHint','cameraWarning','threshold','thresholdValue','resetRoi','roiLabel','startButton','stopButton','progressPanel','progressLabel','progressValue','progress','message','downloads','downloadJson','downloadCsv','downloadVideo','media','preview','previewCanvas','placeholder','previewTitle','previewHint','frameSize','liveDot','jobStatus','jobDetail','processed','frameDetail','inferenceFps','latencyDetail','chartCanvas','chartEmpty','joint','jointReadout'].map(id => [id, byId(id)]));
    const api = new RemoteAPI(window.fetch.bind(window));
    const state = {authenticated:false, models:[], limits:{}, kind:'video', file:null, objectURL:null, stream:null, job:null, busy:false, epoch:0, authEpoch:0, upload:null, pollTimer:null, frame:null, roi:[0,0,1,1], drag:null, samples:[], names:[], previewPending:false, resultPending:false, width:0,height:0};
    const isActive = () => state.busy || Boolean(state.job && !TERMINAL.has(state.job.status));
    const notify = (text, error = false, node = elements.message) => {
      node.textContent = text; node.dataset.error = String(error); node.classList.toggle('hidden', !text);
    };
    const connection = (text, mode = 'ready') => { elements.connectionText.textContent = text; elements.connection.dataset.mode = mode; };
    const csrf = response => { if (response.csrf) api.csrf = response.csrf; };
    const currentModel = () => state.models.find(model => model.id === elements.model.value);
    function updateControls() {
      const active = isActive(), model = currentModel();
      for (const element of [elements.model,elements.threshold,elements.videoTab,elements.cameraTab,elements.videoFile,elements.resetRoi]) element.disabled = active;
      elements.detector.disabled = active || !model || !model.detector_supported || model.detector_ready === false;
      if (model && (!model.detector_supported || model.detector_ready === false)) elements.detector.checked = false;
      elements.startButton.disabled = !state.authenticated || active || !model || !model.ready || (state.kind === 'video' && !state.file) || (state.kind === 'camera' && !window.isSecureContext);
      elements.stopButton.disabled = !(active || state.stream);
      elements.cameraPreviewButton.disabled = active || !window.isSecureContext;
      elements.preview.dataset.selectable = String(!active && Boolean(state.frame));
      elements.previewHint.textContent = active ? (state.kind === 'camera' ? '显示与预测匹配的画面；停止后释放摄像头' : '逐帧分析 · 画面为最近完成的预测') : '开始前可在画面上拖动，限定单猴区域';
      elements.liveDot.dataset.live = String(active && state.kind === 'camera');
    }
    function modelChanged() {
      const model = currentModel();
      elements.modelNote.textContent = model ? (model.ready ? (model.caveat || '模型已在工作站就绪；结果保留原生关节定义。') : '此模型暂未在工作站就绪，请联系管理员。') : '工作站上尚无可用模型。';
      elements.detectorNote.textContent = model && model.detector_supported ? (model.detector_ready === false ? '工作站检测器尚未就绪，请使用手动 ROI。' : '在所选 ROI 内自动定位单猴；使用工作站配置的权重。') : '此模型使用手动 ROI，自动检测不可用。';
      updateControls();
    }
    function setModels(models) {
      const previous = elements.model.value;
      state.models = Array.isArray(models) ? models : Object.values(models || {});
      elements.model.replaceChildren();
      for (const model of state.models) {
        const option = document.createElement('option'); option.value = model.id;
        option.textContent = `${model.label || model.id}${model.ready ? '' : ' · 未就绪'}`;
        option.disabled = !model.ready; elements.model.append(option);
      }
      elements.model.value = state.models.some(model => model.id === previous && model.ready) ? previous : (state.models.find(model => model.ready)?.id || state.models[0]?.id || '');
      modelChanged();
    }
    function clearCamera() {
      camera.stop();
      if (state.stream) for (const track of state.stream.getTracks()) track.stop();
      state.stream = null; elements.media.pause(); elements.media.srcObject = null;
      elements.cameraPreviewButton.textContent = '预览当前电脑的摄像头';
      updateControls();
    }
    function clearPolling() { if (state.pollTimer) window.clearTimeout(state.pollTimer); state.pollTimer = null; }
    function clearFrame() {
      if (state.frame && state.frame.image && typeof state.frame.image.close === 'function') state.frame.image.close();
      state.frame = null; state.width = 0; state.height = 0; elements.placeholder.classList.remove('hidden'); drawPreview();
    }
    function resetJobDisplay() {
      state.samples = []; state.names = []; elements.joint.replaceChildren();
      const option = document.createElement('option'); option.value = '0'; option.textContent = '等待关节定义'; elements.joint.append(option);
      elements.downloads.classList.add('hidden'); elements.progressPanel.classList.add('hidden');
      elements.processed.textContent = '0'; elements.inferenceFps.textContent = '—'; elements.jobStatus.textContent = '待开始'; elements.jobDetail.textContent = '尚未创建任务';
      drawChart();
    }
    function unauthenticated() {
      state.authEpoch++; state.epoch++; state.authenticated = false; state.busy = false;
      if (state.upload) state.upload.abort(); state.upload = null;
      clearPolling(); clearCamera(); state.job = null; api.csrf = '';
      clearFrame(); resetJobDisplay(); state.file=null; elements.videoFile.value=''; elements.fileName.textContent='选择视频文件';
      if(state.objectURL)window.URL.revokeObjectURL(state.objectURL);state.objectURL=null;elements.media.removeAttribute('src');elements.media.load();
      elements.loginPanel.classList.remove('hidden'); elements.application.classList.add('hidden'); elements.logoutButton.classList.add('hidden');
      connection('请输入访问密码', 'waiting'); updateControls();
    }
    async function session() {
      const epoch = state.authEpoch;
      const response = await api.request('/api/session');
      if (epoch !== state.authEpoch) return;
      if (!response.authenticated) { unauthenticated(); return; }
      csrf(response); state.authenticated = true; state.limits = response.limits || {};
      setModels(response.models); elements.fileLimit.textContent = `MP4 / MOV / AVI / MKV · 上限 ${formatBytes(Number(state.limits.max_upload_bytes) || DEFAULT_MAX)}`;
      elements.loginPanel.classList.add('hidden'); elements.application.classList.remove('hidden'); elements.logoutButton.classList.remove('hidden');
      elements.password.value = ''; notify('', false, elements.loginMessage); connection('工作站已连接'); updateControls();
      if(!state.job)await restoreJobs();
    }
    async function restoreJobs() {
      const epoch=state.epoch;
      try{
        const response=await api.request('/api/jobs');
        if(epoch!==state.epoch||!state.authenticated||state.job)return;
        const jobs=Array.isArray(response.jobs)?response.jobs:[];
        if(!jobs.length)return;
        const job=jobs[jobs.length-1];jobPath(job.id);
        // An interrupted upload has no browser File object after a reload.
        if(['created','uploading'].includes(job.status)){
          await api.request(jobPath(job.id),{method:'DELETE'});
          if(epoch===state.epoch)notify('上次上传已中断，请重新选择视频后开始分析。');
          return;
        }
        state.job=job;state.kind=job.kind==='camera'?'camera':'video';
        elements.videoTab.setAttribute('aria-selected',String(state.kind==='video'));elements.cameraTab.setAttribute('aria-selected',String(state.kind==='camera'));
        elements.videoFields.classList.toggle('hidden',state.kind!=='video');elements.cameraFields.classList.toggle('hidden',state.kind!=='camera');
        if(job.model&&state.models.some(model=>model.id===job.model)){elements.model.value=job.model;modelChanged();}
        applyJob(job);
        if(job.kind==='camera'&&!TERMINAL.has(job.status)){
          const stopped=await api.request(jobPath(job.id),{method:'DELETE'});
          if(epoch!==state.epoch)return;
          applyJob({...jobObject(stopped),id:job.id,status:jobObject(stopped).status||'cancelled'});
          notify('已停止刷新前的摄像头任务。重新预览摄像头后，可开始新的分析。');
        }else{
          notify(TERMINAL.has(job.status)?'已恢复上次任务，可继续下载结果。':'已恢复本次会话中的视频任务。');
          pollJob(job.id,epoch);
        }
      }catch(error){if(epoch===state.epoch&&error.status===401)handleError(error);}
    }
    async function handleError(error) {
      if (error.name === 'AbortError') return;
      if (error.status === 401) { unauthenticated(); notify('登录已过期，请重新输入密码。', true, elements.loginMessage); return; }
      notify(error.message || '服务暂时不可用，请稍后重试。', true);
      if (!(error instanceof APIError)) connection('连接出现问题', 'error');
    }
    function roiLabel() {
      const [x1,y1,x2,y2] = state.roi;
      elements.roiLabel.textContent = x1 === 0 && y1 === 0 && x2 === 1 && y2 === 1 ? 'ROI · 全画面' : `ROI · ${Math.round((x2-x1)*100)}% × ${Math.round((y2-y1)*100)}%`;
    }
    function fitCanvas(canvas) {
      const box = canvas.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
      const width = Math.max(1, Math.round(box.width * ratio)), height = Math.max(1, Math.round(box.height * ratio));
      if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
      const context = canvas.getContext('2d'); context.setTransform(ratio,0,0,ratio,0,0); context.clearRect(0,0,box.width,box.height);
      return {context,width:box.width,height:box.height};
    }
    function drawPreview() {
      const {context:ctx,width,height} = fitCanvas(elements.previewCanvas);
      if (!state.frame) return;
      const frame = state.frame, rectangle = containedRect(width,height,frame.width,frame.height);
      ctx.drawImage(frame.image, rectangle.x,rectangle.y,rectangle.width,rectangle.height);
      const toScreen = point => [rectangle.x + point[0] / frame.width * rectangle.width, rectangle.y + point[1] / frame.height * rectangle.height];
      if (frame.result && !frame.serverOverlay) {
        const result = frame.result, points = result.points || result.keypoints_xy_score || [];
        ctx.strokeStyle = '#a2edaa'; ctx.lineWidth = 1.8; ctx.lineCap = 'round';
        for (const [a,b] of result.edges || []) {
          if (!validPoint(points[a],a,result,Number(elements.threshold.value)) || !validPoint(points[b],b,result,Number(elements.threshold.value))) continue;
          const pa=toScreen(points[a]),pb=toScreen(points[b]);ctx.beginPath();ctx.moveTo(...pa);ctx.lineTo(...pb);ctx.stroke();
        }
        for (let index=0; index<points.length; index++) if (validPoint(points[index],index,result,Number(elements.threshold.value))) {
          const point=toScreen(points[index]);ctx.beginPath();ctx.arc(point[0],point[1],3,0,Math.PI*2);ctx.fillStyle='#f2d187';ctx.fill();
        }
      }
      const roi = state.drag ? [Math.min(state.drag.start[0],state.drag.end[0]),Math.min(state.drag.start[1],state.drag.end[1]),Math.max(state.drag.start[0],state.drag.end[0]),Math.max(state.drag.start[1],state.drag.end[1])] : state.roi;
      if (roi.some((value,index) => value !== [0,0,1,1][index]) || state.drag) {
        const x=rectangle.x+roi[0]*rectangle.width,y=rectangle.y+roi[1]*rectangle.height,w=(roi[2]-roi[0])*rectangle.width,h=(roi[3]-roi[1])*rectangle.height;
        ctx.strokeStyle='#edc986';ctx.lineWidth=1.5;ctx.setLineDash([6,4]);ctx.strokeRect(x,y,w,h);ctx.setLineDash([]);
        if (state.drag) {ctx.fillStyle='#e9c17312';ctx.fillRect(x,y,w,h);}
      }
    }
    function setFrame(image,width,height,result=null,serverOverlay=false) {
      if (state.frame && state.frame.image !== image && typeof state.frame.image.close === 'function') state.frame.image.close();
      state.frame={image,width,height,result,serverOverlay};state.width=width;state.height=height;
      elements.placeholder.classList.add('hidden');elements.frameSize.textContent=`${width} × ${height}`;drawPreview();updateControls();
    }
    function addSamples(payload, fallback={}) {
      const trajectory = payload.trajectories || payload.trajectory || payload;
      const entries = Array.isArray(trajectory) ? trajectory : trajectory.samples || [];
      const normalized = entries.map(sample=>normalizeSample(sample,{...fallback,...payload})).filter(Boolean);
      if (!entries.length && (payload.points || payload.keypoints_xy_score)) {
        const sample=normalizeSample(payload,fallback);if(sample)normalized.push(sample);
      }
      if (normalized.length) {
        const merged = new Map(state.samples.map(sample=>[sample.time,sample]));
        for (const sample of normalized) merged.set(sample.time,sample);
        state.samples=[...merged.values()].sort((a,b)=>a.time-b.time).slice(-1800);
        const end=state.samples[state.samples.length-1].time;state.samples=state.samples.filter(sample=>sample.time>=end-15);
      }
      const names = payload.keypoint_names || payload.joint_names || trajectory.keypoint_names;
      if (Array.isArray(names) && JSON.stringify(names)!==JSON.stringify(state.names)) {
        const previous=elements.joint.value;state.names=names.slice();elements.joint.replaceChildren();
        names.forEach((name,index)=>{const option=document.createElement('option');option.value=String(index);option.textContent=`${index+1} · ${name}`;elements.joint.append(option);});
        elements.joint.value=Number(previous)<names.length?previous:'0';
      } else if (!state.names.length && state.samples.length) {
        const count=state.samples[state.samples.length-1].keypoints_xy_score.length;
        if(count)addSamples({keypoint_names:Array.from({length:count},(_,index)=>`关节 ${index+1}`)});
      }
      drawChart();
    }
    function drawChart() {
      const {context:ctx,width,height}=fitCanvas(elements.chartCanvas),padding={left:29,right:10,top:15,bottom:23};
      const plotWidth=width-padding.left-padding.right,plotHeight=height-padding.top-padding.bottom;
      if(plotWidth<1||plotHeight<1)return;
      ctx.font='9px "Segoe UI",sans-serif';ctx.strokeStyle='#e5eadd';ctx.lineWidth=1;ctx.fillStyle='#8a9782';
      [0,.5,1].forEach(value=>{const y=padding.top+(1-value)*plotHeight;ctx.beginPath();ctx.moveTo(padding.left,y);ctx.lineTo(width-padding.right,y);ctx.stroke();ctx.fillText(value.toFixed(1),4,y+3);});
      const samples=state.samples,joint=Number(elements.joint.value)||0,segments=segmentsForJoint(samples,joint,Number(elements.threshold.value));
      elements.chartEmpty.classList.toggle('hidden',segments.length>0);
      if(!samples.length)return;
      const end=samples[samples.length-1].time,start=end-15,x=time=>padding.left+(time-start)/15*plotWidth;
      [15,10,5,0].forEach(seconds=>{const px=x(end-seconds);ctx.fillText(seconds===0?'现在':`−${seconds}s`,px-8,height-5);});
      for(const [key,color] of [['x','#3d7752'],['y','#b78b42']]){
        ctx.strokeStyle=color;ctx.lineWidth=1.65;ctx.beginPath();
        for(const [a,b] of segments){ctx.moveTo(x(a.time),padding.top+(1-a[key])*plotHeight);ctx.lineTo(x(b.time),padding.top+(1-b[key])*plotHeight);}ctx.stroke();
      }
      const latest=samples[samples.length-1],point=latest.keypoints_xy_score[joint];
      elements.jointReadout.textContent=validPoint(point,joint,latest,Number(elements.threshold.value))?`X ${(point[0]/latest.width).toFixed(3)} · Y ${(point[1]/latest.height).toFixed(3)}`:'低分、缺失或超过 1 秒时断开';
    }
    async function imageFromBlob(blob) {
      if(window.createImageBitmap)return window.createImageBitmap(blob);
      return new Promise((resolve,reject)=>{const url=window.URL.createObjectURL(blob),image=new window.Image();image.onload=()=>{window.URL.revokeObjectURL(url);resolve(image);};image.onerror=()=>{window.URL.revokeObjectURL(url);reject(new Error('无法载入分析画面'));};image.src=url;});
    }
    async function cameraCapture() {
      const video=elements.media;
      if(!state.stream||!video.videoWidth||video.readyState<2)throw new Error('摄像头没有可用画面，请检查权限或设备连接');
      const scale=Math.min(1,960/video.videoWidth),width=Math.max(1,Math.round(video.videoWidth*scale)),height=Math.max(1,Math.round(video.videoHeight*scale));
      const snapshot=document.createElement('canvas');snapshot.width=width;snapshot.height=height;snapshot.getContext('2d').drawImage(video,0,0,width,height);
      const blob=await new Promise(resolve=>snapshot.toBlob(resolve,'image/jpeg',.78));
      if(!blob)throw new Error('浏览器无法编码摄像头画面');
      return {blob,image:snapshot,width,height};
    }
    const camera=new CameraRunner(api,{
      onResult:async(result,snapshot,latency)=>{
        const epoch=state.epoch;
        let image=snapshot.image,serverOverlay=false;
        if(typeof result.jpeg==='string'&&result.jpeg){image=await imageFromBlob(jpegBlob(result.jpeg,window.atob.bind(window)));serverOverlay=true;}
        if(epoch!==state.epoch||!state.job||!camera.running){if(image.close)image.close();return;}
        setFrame(image,Number(result.width||result.frame_width||snapshot.width),Number(result.height||result.frame_height||snapshot.height),result,serverOverlay);
        addSamples(result,{width:snapshot.width,height:snapshot.height,timestamp:performance.now()/1000});
        elements.latencyDetail.textContent=`本次往返 ${Math.round(latency)} ms`;
        elements.processed.textContent=String(result.processed??result.sequence??0);
        elements.inferenceFps.textContent=finite(result.inference_fps)?result.inference_fps.toFixed(1):(1000/Math.max(1,latency)).toFixed(1);
        elements.frameDetail.textContent='串行发送 · 同时最多一帧';
        elements.jobStatus.textContent='分析中';
      },
      onError:error=>{handleError(error);stopJob(false);}
    });
    async function openCamera() {
      if(!window.isSecureContext)throw new Error('浏览器摄像头需要 HTTPS。请通过管理员提供的 HTTPS 地址访问。');
      if(!window.navigator.mediaDevices||!window.navigator.mediaDevices.getUserMedia)throw new Error('当前浏览器不支持摄像头，请使用新版 Chrome、Edge 或 Firefox');
      if(state.stream)return state.stream;
      const epoch=state.epoch;
      let stream;
      try{stream=await window.navigator.mediaDevices.getUserMedia({video:{width:{ideal:1280},height:{ideal:720},facingMode:'environment'},audio:false});}
      catch(error){if(error.name==='NotAllowedError')throw new Error('摄像头权限被拒绝，请在浏览器地址栏允许访问后重试');if(error.name==='NotFoundError')throw new Error('这台电脑没有可用摄像头');throw new Error('无法开启摄像头，请检查是否被其他程序占用');}
      if(epoch!==state.epoch||!state.authenticated||state.kind!=='camera'){for(const track of stream.getTracks())track.stop();throw aborted();}
      state.stream=stream;elements.media.removeAttribute('src');elements.media.srcObject=stream;
      for(const track of stream.getTracks())if(track.addEventListener)track.addEventListener('ended',()=>{
        if(state.stream===stream){notify('摄像头连接已结束，请检查设备或权限。',true);stopJob(false);}
      },{once:true});
      await elements.media.play();
      if(epoch!==state.epoch||!state.stream){for(const track of stream.getTracks())track.stop();throw aborted();}
      const snapshot=await cameraCapture();setFrame(snapshot.image,snapshot.width,snapshot.height);
      elements.cameraPreviewButton.textContent='摄像头已就绪';elements.cameraHint.textContent='预览已开启。点击“开始分析”后才向工作站发送画面。';
      updateControls();return stream;
    }
    function progress(label,value,text) {
      elements.progressPanel.classList.remove('hidden');elements.progressLabel.textContent=label;
      if(value===null){elements.progress.removeAttribute('value');elements.progressValue.textContent=text||'…';}
      else{elements.progress.value=clamp(value,0,1);elements.progressValue.textContent=text||`${Math.round(value*100)}%`;}
    }
    function applyJob(payload) {
      const job=jobObject(payload);if(!job||!state.job||job.id!==state.job.id)return;
      state.job={...state.job,...job};
      const status=state.job.status;
      elements.jobStatus.textContent=STATUS_LABELS[status]||'准备中';
      elements.jobDetail.textContent=state.job.kind==='camera'?'浏览器摄像头 · 当前会话':(status==='queued'?'等待工作站中的前序任务':`任务 ${state.job.id.slice(0,8)}`);
      const processed=Number(job.processed??job.processed_frames??job.frames_processed??0),total=Number(job.total_frames??job.total??0);
      if(state.job.kind!=='camera'){
        elements.processed.textContent=String(processed);elements.frameDetail.textContent=total?`共 ${total.toLocaleString()} 帧`:'逐帧处理上传的视频';
        if(finite(job.inference_fps))elements.inferenceFps.textContent=job.inference_fps.toFixed(1);
        if(status==='uploading'||status==='created'){}else if(status==='queued')progress('任务已上传，等待分析',null,'排队中');
        else if(status==='loading'||status==='loading_model'||status==='starting')progress('正在载入工作站模型',null,'载入中');
        else if(TERMINAL.has(status))progress(status==='completed'||status==='done'?'分析完成':'任务已结束',status==='completed'||status==='done'?1:(total?processed/total:0));
        else{const amount=finite(job.progress)?(job.progress>1?job.progress/100:job.progress):(total?processed/total:0);progress('逐帧分析中',total||finite(job.progress)?amount:null);}
      }
      if(job.error)notify(String(job.error),true);
      const downloads=job.downloads||{};
      if(TERMINAL.has(status)&&(processed>0||Object.values(downloads).some(Boolean))){
        elements.downloads.classList.remove('hidden');
        for(const [key,node] of [['json',elements.downloadJson],['csv',elements.downloadCsv],['video',elements.downloadVideo]]){
          node.href=jobPath(job.id,`/download/${key}`);node.classList.toggle('hidden',key==='video'?!downloads.video:downloads[key]===false);
        }
      }
      updateControls();
    }
    async function refreshPreview(id,epoch) {
      if(state.previewPending)return;state.previewPending=true;
      try{
        const response=await window.fetch(jobPath(id,`/preview.jpg?t=${Date.now()}`),{credentials:'same-origin',cache:'no-store'});
        if(response.status===401){if(epoch===state.epoch)unauthenticated();return;}
        if(!response.ok)return;
        const image=await imageFromBlob(await response.blob());
        if(epoch!==state.epoch||!state.job||state.job.id!==id){if(image.close)image.close();return;}
        setFrame(image,image.width||image.naturalWidth,image.height||image.naturalHeight,null,true);
      }catch(_){/* A preview is optional while the first frame is being processed. */}
      finally{state.previewPending=false;}
    }
    async function refreshResult(id,epoch) {
      if(state.resultPending)return;state.resultPending=true;
      try{const result=await api.request(jobPath(id,'/result'));if(epoch===state.epoch&&state.job&&state.job.id===id)addSamples(result,{width:state.width,height:state.height});}
      catch(error){if(error.status===401)handleError(error);}
      finally{state.resultPending=false;}
    }
    async function pollJob(id,epoch) {
      if(epoch!==state.epoch||!state.authenticated||!state.job||state.job.id!==id)return;
      try{
        const response=await api.request(jobPath(id));
        if(epoch!==state.epoch||!state.job||state.job.id!==id)return;
        applyJob(response);connection('工作站已连接');
        if(state.job.kind==='video')await Promise.allSettled([refreshPreview(id,epoch),refreshResult(id,epoch)]);
        else if(state.job.status==='running'&&!camera.running&&state.stream){
          camera.start({id,stream:state.stream,capture:cameraCapture});
          notify('摄像头分析已开始，画面正在发送到工作站。');
        }
        if(epoch!==state.epoch||!state.job)return;
        if(TERMINAL.has(state.job.status)){
          state.busy=false;if(state.job.kind==='camera')clearCamera();updateControls();return;
        }
      }catch(error){if(epoch!==state.epoch)return;if(error.status===401){handleError(error);return;}connection('连接中断，正在重试','error');notify(error.message,true);}
      if(epoch===state.epoch)state.pollTimer=window.setTimeout(()=>pollJob(id,epoch),1000);
    }
    async function startJob() {
      if(isActive())return;
      notify('');clearPolling();resetJobDisplay();
      const epoch=++state.epoch;state.busy=true;state.job=null;updateControls();
      try{
        if(state.kind==='video'){
          const limit=Number(state.limits.max_upload_bytes)||DEFAULT_MAX;
          if(!state.file||state.file.size<=0)throw new Error('请先选择视频文件');
          if(state.file.size>limit)throw new Error(`视频超过 ${formatBytes(limit)} 的上传上限`);
        }else await openCamera();
        if(epoch!==state.epoch)return;
        const response=await api.json('/api/jobs','POST',{kind:state.kind,model:elements.model.value,threshold:Number(elements.threshold.value),roi:state.roi.slice(),detector:elements.detector.checked,record:true});
        const job=jobObject(response);jobPath(job.id);
        if(epoch!==state.epoch){await api.request(jobPath(job.id),{method:'DELETE'}).catch(()=>{});return;}
        state.job=job;state.job.kind=state.kind;state.job.status=job.status||'created';applyJob(job);
        if(state.kind==='video'){
          const controller=new AbortController();state.upload=controller;
          progress('正在上传视频',0);
          const final=await uploadVideo(api,job.id,state.file,{chunkBytes:Number(state.limits.chunk_bytes)||DEFAULT_CHUNK,maxBytes:Number(state.limits.max_upload_bytes)||DEFAULT_MAX,signal:controller.signal,onProgress:(received,total)=>{if(epoch===state.epoch)progress('正在上传视频',received/total,`${formatBytes(received)} / ${formatBytes(total)}`);}});
          if(epoch!==state.epoch)return;
          state.upload=null;state.busy=false;
          const finalJob=jobObject(final);
          applyJob(finalJob.id?finalJob:{id:job.id,status:'queued'});
          notify('视频已上传。此页可查看任务进度，完成后下载结果。');
        }else{
          state.busy=false;applyJob(state.job);
          notify('正在准备摄像头任务，模型就绪后开始发送画面。');
        }
        updateControls();pollJob(job.id,epoch);
      }catch(error){
        if(epoch!==state.epoch)return;
        const id=state.job&&state.job.id;
        state.busy=false;state.upload=null;
        if(id){await api.request(jobPath(id),{method:'DELETE'}).catch(()=>{});if(state.job)state.job.status='failed';}
        if(state.kind==='camera')clearCamera();
        handleError(error);updateControls();
      }
    }
    async function stopJob(showMessage=true) {
      const id=state.job&&state.job.id;
      const active=state.job&&!TERMINAL.has(state.job.status);
      state.epoch++;state.busy=false;
      if(state.upload)state.upload.abort();state.upload=null;
      clearPolling();clearCamera();
      if(active&&id){
        state.job.status='stopping';updateControls();elements.jobStatus.textContent='停止中';
        try{
          const response=await api.request(jobPath(id),{method:'DELETE'});
          if(!state.job||state.job.id!==id)return;
          applyJob({...jobObject(response),id,status:jobObject(response).status||'cancelled'});
          if(!TERMINAL.has(state.job.status))pollJob(id,state.epoch);
        }catch(error){handleError(error);if(state.job)state.job.status='cancelled';}
      }
      if(showMessage)notify('已停止发送画面并释放摄像头。任务记录可在本次会话中下载。');updateControls();
    }
    async function changeKind(kind) {
      if(isActive()||state.kind===kind)return;
      state.epoch++;clearCamera();clearFrame();clearPolling();state.kind=kind;state.job=null;resetJobDisplay();notify('');
      elements.videoTab.setAttribute('aria-selected',String(kind==='video'));elements.cameraTab.setAttribute('aria-selected',String(kind==='camera'));
      elements.videoFields.classList.toggle('hidden',kind!=='video');elements.cameraFields.classList.toggle('hidden',kind!=='camera');
      elements.previewTitle.textContent=kind==='video'?'上传视频 · 分析画面':'浏览器摄像头 · 分析画面';
      if(kind==='video'&&state.file)await previewFile();updateControls();
    }
    async function previewFile() {
      if(!state.file||state.kind!=='video')return;
      const epoch=state.epoch;
      if(state.objectURL)window.URL.revokeObjectURL(state.objectURL);
      state.objectURL=window.URL.createObjectURL(state.file);elements.media.srcObject=null;elements.media.src=state.objectURL;elements.media.load();
      elements.media.onloadeddata=()=>{
        if(epoch!==state.epoch||state.kind!=='video'||isActive())return;
        const video=elements.media,width=video.videoWidth,height=video.videoHeight;if(!width||!height)return;
        const snapshot=document.createElement('canvas');snapshot.width=width;snapshot.height=height;snapshot.getContext('2d').drawImage(video,0,0);setFrame(snapshot,width,height);
      };
      elements.media.onerror=()=>{if(epoch===state.epoch&&!isActive())notify('浏览器无法预览此视频编码。你仍可上传，由工作站尝试解码。');};
    }
    elements.loginForm.addEventListener('submit',async event=>{
      event.preventDefault();if(elements.loginButton.disabled)return;
      elements.loginButton.disabled=true;notify('',false,elements.loginMessage);
      try{await api.login(elements.password.value);await session();}
      catch(error){notify(error.message||'连接失败，请检查访问地址与密码。',true,elements.loginMessage);connection('登录未完成','error');}
      finally{elements.loginButton.disabled=false;}
    });
    elements.logoutButton.addEventListener('click',async()=>{
      elements.logoutButton.disabled=true;
      await stopJob(false);
      try{await api.request('/api/logout',{method:'POST'});unauthenticated();}
      catch(error){handleError(error);notify('退出请求未完成，请重试。摄像头已释放。',true);}
      finally{elements.logoutButton.disabled=false;}
    });
    elements.videoTab.addEventListener('click',()=>changeKind('video'));
    elements.cameraTab.addEventListener('click',()=>changeKind('camera'));
    elements.model.addEventListener('change',modelChanged);
    elements.threshold.addEventListener('input',()=>{elements.thresholdValue.textContent=Number(elements.threshold.value).toFixed(2);drawChart();drawPreview();});
    elements.videoFile.addEventListener('change',()=>{
      if(isActive())return;
      state.epoch++;state.file=elements.videoFile.files[0]||null;state.job=null;state.roi=[0,0,1,1];roiLabel();resetJobDisplay();clearFrame();notify('');
      elements.fileName.textContent=state.file?state.file.name:'选择视频文件';
      if(state.file){const limit=Number(state.limits.max_upload_bytes)||DEFAULT_MAX;if(state.file.size>limit)notify(`此文件为 ${formatBytes(state.file.size)}，超过 ${formatBytes(limit)} 的上限。`,true);previewFile();}
      updateControls();
    });
    elements.cameraPreviewButton.addEventListener('click',async()=>{try{notify('');await openCamera();}catch(error){handleError(error);}});
    elements.startButton.addEventListener('click',startJob);elements.stopButton.addEventListener('click',()=>stopJob());
    elements.resetRoi.addEventListener('click',()=>{if(!isActive()){state.roi=[0,0,1,1];roiLabel();drawPreview();}});
    elements.joint.addEventListener('change',drawChart);
    const pointerPosition=event=>{
      if(!state.frame)return null;
      const box=elements.previewCanvas.getBoundingClientRect();return pointInFrame(event.clientX-box.left,event.clientY-box.top,containedRect(box.width,box.height,state.frame.width,state.frame.height));
    };
    elements.previewCanvas.addEventListener('pointerdown',event=>{
      if(isActive()||!state.frame||event.button!==0)return;
      const point=pointerPosition(event);if(!point)return;
      state.drag={start:point,end:point};elements.previewCanvas.setPointerCapture(event.pointerId);drawPreview();event.preventDefault();
    });
    elements.previewCanvas.addEventListener('pointermove',event=>{if(!state.drag)return;const point=pointerPosition(event);if(point){state.drag.end=point;drawPreview();}});
    const finishROI=event=>{
      if(!state.drag)return;
      const {start,end}=state.drag;state.drag=null;
      if(Math.abs(start[0]-end[0])>=.015&&Math.abs(start[1]-end[1])>=.015)state.roi=[Math.min(start[0],end[0]),Math.min(start[1],end[1]),Math.max(start[0],end[0]),Math.max(start[1],end[1])];
      if(elements.previewCanvas.hasPointerCapture(event.pointerId))elements.previewCanvas.releasePointerCapture(event.pointerId);roiLabel();drawPreview();
    };
    elements.previewCanvas.addEventListener('pointerup',finishROI);elements.previewCanvas.addEventListener('pointercancel',()=>{state.drag=null;drawPreview();});
    window.addEventListener('resize',()=>{drawPreview();drawChart();});
    window.addEventListener('pagehide',()=>{state.epoch++;if(state.upload)state.upload.abort();clearPolling();clearCamera();if(state.objectURL)window.URL.revokeObjectURL(state.objectURL);});
    window.addEventListener('online',()=>{if(state.authenticated){connection('重新连接工作站…','waiting');if(state.job&&!TERMINAL.has(state.job.status)){clearPolling();pollJob(state.job.id,state.epoch);}}});
    elements.cameraWarning.classList.toggle('hidden',Boolean(window.isSecureContext));
    session().catch(error=>{unauthenticated();notify(error.message||'暂时无法连接工作站，请检查网络后刷新。',true,elements.loginMessage);connection('连接未完成','error');});
    return {state,api,startJob,stopJob,session};
  }
  return {RemoteAPI,APIError,CameraRunner,uploadVideo,jobPath,formatBytes,jpegBlob,containedRect,pointInFrame,validPoint,normalizeSample,segmentsForJoint,TERMINAL,mount};
});
