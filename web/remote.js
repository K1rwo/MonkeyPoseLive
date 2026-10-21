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
  // Keys remain the source Chinese text so new server messages never become HTML.
  // Translation changes presentation only; job ownership, ROI and camera state stay intact.
  const JAPANESE = {
    '语言':'言語','MonkeyPoseLive · 在线骨骼分析':'MonkeyPoseLive · オンライン動作解析','MonkeyPoseLive 首页':'MonkeyPoseLive ホーム',
    '在线骨骼与关节轨迹':'オンライン骨格・顔解析','在线骨骼、面部与轨迹':'オンライン骨格・顔・軌跡','连接服务…':'サービスに接続中…','退出登录':'ログアウト',
    'Connect to your inference workstation':'推論ワークステーションへ接続','让另一台电脑，也能看见运动':'別のパソコンから、動きを解析',
    '上传视频或使用当前电脑的摄像头。模型在部署电脑上运行，浏览器直接查看骨骼和关节轨迹。':'動画をアップロードするか、このパソコンのカメラを使用します。モデルはホスト側で実行され、ブラウザーで骨格・顔・軌跡を確認できます。',
    '访问密码':'アクセスパスワード','输入管理员提供的访问密码':'管理者から受け取ったパスワード','连接工作站':'ワークステーションに接続',
    '请通过管理员提供的 HTTPS 地址访问。视频和摄像头画面会传输到部署电脑处理。':'管理者が提供する HTTPS アドレスからアクセスしてください。動画とカメラ画像はホストに送信され、処理されます。',
    'Hosted inference workspace':'ホスト型推論ワークスペース','从画面，走向运动轨迹':'映像から、動きと顔の変化へ','浏览器连接 · 工作站推理 · 结果留在受控目录':'ブラウザーで接続 · ホストで推論 · 結果は管理対象フォルダーに保存',
    '单工作站 · 按顺序处理任务':'1 台のホスト · 順番に処理','骨骼分析画面':'解析プレビュー','分析画面':'解析画面','等待输入':'入力待ち',
    '分析画面；任务开始前可拖动选择目标区域':'解析画面。開始前にドラッグして対象領域を指定できます',
    '准备一段猴子视频':'サルの動画を準備','选择文件，或连接这台电脑的摄像头':'動画を選択するか、このパソコンのカメラを接続',
    '开始前可在画面上拖动，限定单猴区域':'開始前に画面をドラッグし、1 頭の対象領域を指定','ROI · 全画面':'ROI · 画面全体',
    '任务指标':'処理指標','任务状态':'タスク状態','待开始':'開始待ち','尚未创建任务':'タスク未作成','已分析画面':'解析済み画像','帧':'フレーム',
    '保留模型原生骨架':'モデル固有のキーポイント定義','处理速度':'処理速度','含网络与推理等待':'通信と推論の待機を含む',
    '关节轨迹':'関節軌跡','面部关键点轨迹':'顔ランドマークの軌跡','最近 15 秒 · 归一化画面位置':'直近 15 秒 · 正規化した画像内位置','选择关节':'キーポイントを選択','等待关节定义':'キーポイント定義待ち',
    '所选关节横向和纵向位置随时间变化':'選択したキーポイントの X・Y 座標の時間変化','分析开始后显示关节位置变化':'解析開始後にキーポイントの位置変化を表示',
    'X 横向位置':'X 横方向','Y 纵向位置':'Y 縦方向','低分或缺失时断开':'低スコア・欠損時は接続しません','低分、缺失或超过 1 秒时断开':'低スコア・欠損・1 秒超の間隔では接続しません',
    '骨骼与轨迹为自动预测，各模型使用原生关节定义。分数未经校准，不代表解剖准确率；X-Pose 的对象分数不能评价单个关节。表情分类为探索性输出，不代表动物的真实内部情感。':'骨格と軌跡は自動予測で、各モデル固有のキーポイント定義を使用します。スコアは未校正で、解剖学的精度を示しません。X-Pose の物体スコアでは個々の関節を評価できません。表情分類は探索的な出力であり、動物の実際の内的感情を表しません。',
    '面部表情分析':'顔表情の解析','模型输出 · 探索性使用':'モデル出力 · 探索的な利用','等待面部画面':'顔画像を待機中',
    '表情类别是模型的视觉分类，不代表动物的真实内部情感。遮挡、姿态和画质可能影响结果。':'表情カテゴリはモデルによる画像分類であり、動物の実際の内的感情を表しません。遮蔽、顔の向き、画質の影響を受けます。',
    '模型分数（未经校准）':'モデルスコア（未校正）','未检测到可用面部':'利用可能な顔を検出できませんでした','表情分类不输出关节轨迹。':'表情分類は関節軌跡を出力しません。',
    '分析设置':'解析設定','输入方式':'入力方法','上传视频':'動画アップロード','浏览器摄像头':'ブラウザーカメラ','选择视频文件':'動画ファイルを選択',
    '完整视频上传后进入队列，逐帧分析。浏览器不支持预览的编码仍可尝试上传。':'動画全体をアップロード後、キューに入り全フレームを解析します。ブラウザーでプレビューできない形式もアップロードを試せます。',
    '预览当前电脑的摄像头':'このパソコンのカメラをプレビュー','由浏览器申请摄像头权限，使用你正在操作的电脑上的摄像头。':'ブラウザーがカメラの許可を求めます。操作中のパソコンのカメラを使用します。',
    '浏览器摄像头需要 HTTPS 安全连接。请使用管理员提供的 HTTPS 地址；本机 localhost 也可用于测试。':'ブラウザーカメラには HTTPS 接続が必要です。管理者の HTTPS アドレスを使用してください。ホスト上の localhost でもテストできます。',
    '分析模型':'解析モデル','正在读取工作站模型状态…':'ホストのモデル状態を取得中…','自动定位猴子':'サルを自動検出','自动定位面部':'顔を自動検出',
    '使用工作站配置的检测器；关闭时使用所选 ROI。':'ホストに設定された検出器を使用します。オフの場合は指定した ROI を使用します。',
    '显示分数阈值':'表示スコアのしきい値','低于阈值的关节不连接轨迹。':'しきい値未満のキーポイントは軌跡を接続しません。','目标区域 ROI':'対象領域 ROI','重置':'リセット',
    '开始前拖动画面选择目标区域。任务中设置保持固定。':'開始前にドラッグして対象領域を指定します。処理中は設定を固定します。',
    '上传的视频和分析结果存放在部署电脑。摄像头仅在开始分析后发送画面；停止或退出登录会释放摄像头。由管理员管理保存期限。':'アップロード動画と解析結果はホストに保存されます。カメラ画像は解析開始後に送信され、停止・ログアウト時にカメラを解放します。保存期間は管理者が管理します。',
    '开始分析':'解析開始','停止':'停止','准备任务':'タスク準備','下载分析结果':'解析結果をダウンロード','骨骼视频':'解析動画','分析视频':'解析動画',
    '含自动预测坐标与运行信息，请在研究使用前核查。':'自動予測の座標と実行情報を含みます。研究利用前に確認してください。','MonkeyPoseLive · 远程访问':'MonkeyPoseLive · リモートアクセス','模型在工作站上运行 · 客户端无需安装推理环境':'モデルはホストで実行 · クライアントに推論環境の導入は不要',
    '显示与预测匹配的画面；停止后释放摄像头':'予測と一致する画像を表示 · 停止後にカメラを解放','逐帧分析 · 画面为最近完成的预测':'全フレームを解析 · 最新の完了済み予測を表示',
    '模型已在工作站就绪；结果保留原生关节定义。':'ホストのモデルを使用できます。モデル固有の定義で出力します。','此模型暂未在工作站就绪，请联系管理员。':'このモデルはホストで未準備です。管理者に連絡してください。','工作站上尚无可用模型。':'ホストに利用可能なモデルがありません。',
    '工作站检测器尚未就绪，请使用手动 ROI。':'ホストの検出器が未準備です。手動 ROI を使用してください。','在所选 ROI 内自动定位单猴；使用工作站配置的权重。':'指定した ROI 内の 1 頭をホストの検出器で自動検出します。','此模型使用手动 ROI，自动检测不可用。':'このモデルは手動 ROI を使用します。自動検出は使用できません。',
    '面部模型在所选 ROI 内定位面部。':'顔モデルは指定した ROI 内から顔を検出します。','面部模型自行定位面部，建议将 ROI 限定到单猴头部。':'顔モデルが顔を検出します。ROI は 1 頭の頭部に絞ることを推奨します。',
    '请输入访问密码':'アクセスパスワードを入力','工作站已连接':'ホストに接続済み','上次上传已中断，请重新选择视频后开始分析。':'前回のアップロードが中断されました。動画を選び直して解析を開始してください。',
    '已停止刷新前的摄像头任务。重新预览摄像头后，可开始新的分析。':'再読み込み前のカメラ処理を停止しました。カメラを再度プレビューして新しい解析を開始できます。',
    '已恢复上次任务，可继续下载结果。':'前回のタスクを復元しました。結果をダウンロードできます。','已恢复本次会话中的视频任务。':'このセッションの動画タスクを復元しました。',
    '登录已过期，请重新输入密码。':'ログインの有効期限が切れました。パスワードを再入力してください。','服务暂时不可用，请稍后重试。':'サービスを利用できません。しばらくしてから再試行してください。','连接出现问题':'接続に問題があります',
    '现在':'現在','无法载入分析画面':'解析画像を読み込めません','摄像头没有可用画面，请检查权限或设备连接':'カメラ画像を取得できません。許可と機器の接続を確認してください。','浏览器无法编码摄像头画面':'カメラ画像をエンコードできません',
    '串行发送 · 同时最多一帧':'逐次送信 · 同時に 1 フレームまで','分析中':'解析中','浏览器摄像头需要 HTTPS。请通过管理员提供的 HTTPS 地址访问。':'ブラウザーカメラには HTTPS が必要です。管理者の HTTPS アドレスからアクセスしてください。',
    '当前浏览器不支持摄像头，请使用新版 Chrome、Edge 或 Firefox':'このブラウザーはカメラに対応していません。最新の Chrome、Edge、Firefox を使用してください。','摄像头权限被拒绝，请在浏览器地址栏允许访问后重试':'カメラの許可が拒否されました。アドレスバーで許可してから再試行してください。',
    '这台电脑没有可用摄像头':'このパソコンに利用可能なカメラがありません','无法开启摄像头，请检查是否被其他程序占用':'カメラを開始できません。他のアプリで使用中でないか確認してください。','摄像头连接已结束，请检查设备或权限。':'カメラの接続が終了しました。機器と許可を確認してください。',
    '摄像头已就绪':'カメラ準備完了','预览已开启。点击“开始分析”后才向工作站发送画面。':'プレビュー中です。「解析開始」を押すとホストへ画像を送信します。',
    '待上传':'アップロード待ち','上传中':'アップロード中','排队中':'キュー待ち','载入模型':'モデル読み込み','启动中':'起動中','已完成':'完了','分析失败':'解析失敗','停止中':'停止中','已停止':'停止済み','准备中':'準備中',
    '浏览器摄像头 · 当前会话':'ブラウザーカメラ · 現在のセッション','等待工作站中的前序任务':'ホスト上の先行タスクを待機','逐帧处理上传的视频':'動画の全フレームを処理',
    '任务已上传，等待分析':'アップロード済み · 解析待ち','正在载入工作站模型':'ホストのモデルを読み込み中','载入中':'読み込み中','分析完成':'解析完了','任务已结束':'タスク終了','逐帧分析中':'全フレームを解析中',
    '摄像头分析已开始，画面正在发送到工作站。':'カメラ解析を開始しました。ホストへ画像を送信しています。','连接中断，正在重试':'接続が中断しました · 再試行中','请先选择视频文件':'動画ファイルを選択してください','正在上传视频':'動画をアップロード中',
    '视频已上传。此页可查看任务进度，完成后下载结果。':'動画をアップロードしました。この画面で進捗を確認し、完了後に結果をダウンロードできます。','正在准备摄像头任务，模型就绪后开始发送画面。':'カメラタスクを準備中です。モデルの準備完了後に画像を送信します。',
    '已停止发送画面并释放摄像头。任务记录可在本次会话中下载。':'画像の送信を停止し、カメラを解放しました。このセッション内でタスク記録をダウンロードできます。','上传视频 · 分析画面':'アップロード動画 · 解析画面','浏览器摄像头 · 分析画面':'ブラウザーカメラ · 解析画面',
    '浏览器无法预览此视频编码。你仍可上传，由工作站尝试解码。':'この形式はブラウザーでプレビューできません。アップロードし、ホストでのデコードを試せます。','连接失败，请检查访问地址与密码。':'接続できません。アドレスとパスワードを確認してください。','登录未完成':'ログイン未完了',
    '退出请求未完成，请重试。摄像头已释放。':'ログアウトできませんでした。再試行してください。カメラは解放済みです。','重新连接工作站…':'ホストに再接続中…','暂时无法连接工作站，请检查网络后刷新。':'ホストに接続できません。ネットワークを確認して再読み込みしてください。','连接未完成':'接続未完了',
    '操作已停止':'操作を停止しました','无效的 API 路径':'無効な API パス','服务返回了无效的任务编号':'サーバーから無効なタスク ID が返されました','请选择非空视频文件':'空でない動画ファイルを選択してください','上传进度与服务器不一致，请重新创建任务':'アップロード位置がサーバーと一致しません。タスクを作り直してください。','服务返回的画面序号不匹配，分析已停止':'フレーム番号が一致しないため、解析を停止しました','服务返回的分析画面无效':'サーバーの解析画像が無効です',
    '密码不正确':'パスワードが正しくありません','登录尝试过多，请稍后再试':'ログイン試行が多すぎます。しばらくしてから再試行してください。','会话数量达到上限':'セッション数が上限に達しました','请先登录':'先にログインしてください','任务参数无效':'タスクのパラメーターが無効です','模型尚未准备好':'モデルの準備ができていません','未知输入类型':'不明な入力形式です','ROI须为0–1范围的有效矩形':'ROI は 0〜1 の範囲で有効な長方形を指定してください','阈值须在0–1之间':'しきい値は 0〜1 の範囲で指定してください','开关参数须为布尔值':'スイッチの値は真偽値で指定してください','此模型不支持自动定位':'このモデルは自動検出に対応していません','任务数量达到上限':'タスク数が上限に達しました','本机存储配额不足，请管理员归档旧任务':'ホストの保存容量が不足しています。管理者に過去のタスクの整理を依頼してください。','任务已停止':'タスクは停止済みです','任务队列已满':'タスクキューが満杯です','任务不存在':'タスクが見つかりません','任务未能完成；请由主机管理员检查本机日志及输入格式':'タスクを完了できませんでした。ホストの管理者にログと入力形式の確認を依頼してください。',
    'Host不被允许':'許可されていないホストです','Origin不被允许':'許可されていないオリジンです','需要同源请求':'同一オリジンのリクエストが必要です','跨站请求被拒绝':'クロスサイトのリクエストを拒否しました','需要单一有效Content-Length':'有効な Content-Length が 1 つ必要です','请求超过大小限制':'リクエストがサイズ上限を超えています','上传不完整':'アップロードが不完全です','需要application/json':'application/json が必要です','JSON无效':'JSON が無効です','需要JSON对象':'JSON オブジェクトが必要です','CSRF验证失败':'CSRF 検証に失敗しました','页面不存在':'ページが見つかりません','管理验证失败':'管理者認証に失敗しました','结果尚未就绪':'結果の準備ができていません','结果文件尚未就绪':'結果ファイルの準備ができていません','请求参数无效':'リクエストのパラメーターが無効です','服务器无法完成请求，请联系主机管理员':'処理を完了できません。ホストの管理者に連絡してください。',
    '任务不接受此上传或有上传正在进行':'このタスクはアップロードを受け付けていないか、別のアップロードが進行中です','任务已结束上传':'このタスクのアップロードは終了済みです','上传参数无效':'アップロードのパラメーターが無効です','需要完整视频长度':'動画全体のサイズが必要です','上传视频超出512MiB限制或结束标记无效':'動画が 512 MiB を超えているか、終了マーカーが無効です','分块偏移与当前上传不一致':'分割データの位置がアップロード済み位置と一致しません','分块长度与结束标记不一致':'分割データの長さと終了マーカーが一致しません','本机存储已满':'ホストの保存容量が不足しています','摄像头模型尚未就绪或上一帧仍在处理':'カメラ用モデルの準備中、または前のフレームを処理中です','摄像头参数无效':'カメラのパラメーターが無効です','帧编号和时间须有效递增':'フレーム番号と時刻は有効な増加値にしてください','需要image/jpeg':'image/jpeg が必要です','推理响应超时':'推論の応答がタイムアウトしました',
    'neutral':'中立','lipsmack':'リップスマック','threat':'威嚇様表情','fearful':'恐怖様表情',
    '中性':'中立','咂嘴':'リップスマック','威胁样面部表现':'威嚇様表情','恐惧样面部表现':'恐怖様表情',
    'PrimateFace · 面部关键点':'PrimateFace · 顔ランドマーク','猕猴面部表情CNN':'マカク顔表情CNN',
    'Only 2D; released17 includes two unvalidated eye channels. Display excludes them.':'2D のみ。公開 17 点には未検証の眼の 2 点が含まれるため、表示では除外します。',
    'Original AP10K expert3 + associate head2; original animal anatomy, not renamed COCO.':'元の AP10K expert3 と associate head2 を使用します。動物固有の解剖学的定義を保持し、COCO に名称変更しません。',
    'Object score only; target identity unverified; FP32 original resize800/max1333.':'物体スコアのみ。対象個体の同一性は未検証です。FP32、元の resize800/max1333 設定を使用します。',
    'User-supplied17-point TensorFlow checkpoint; training exclusion must be established separately.':'ユーザー提供の TensorFlow 17 点モデルです。評価動画が学習に含まれないことは別途確認が必要です。',
    'Facial-display predictions are exploratory; no internal affect or anatomical accuracy validation.':'顔表情の予測は探索的な出力です。内的感情や解剖学的精度の検証は行っていません。',
    'Failed to fetch':'ネットワークに接続できません。接続を確認して再試行してください。','Load failed':'読み込みに失敗しました。接続を確認してください。','NetworkError when attempting to fetch resource.':'通信エラーが発生しました。ネットワーク接続を確認してください。'
  };
  const CHINESE_CLASSES = {neutral:'中性',lipsmack:'咂嘴',threat:'威胁样面部表现',fearful:'恐惧样面部表现'};
  function translate(text, language='zh') {
    text=String(text ?? '');
    if(language!=='ja')return text;
    if(Object.prototype.hasOwnProperty.call(JAPANESE,text))return JAPANESE[text];
    const patterns = [
      [/^服务请求失败（(\d+)）$/,m=>`サービスへのリクエストが失敗しました（${m[1]}）`],
      [/^视频超过 (.+) 的上传上限$/,m=>`動画がアップロード上限（${m[1]}）を超えています`],
      [/^MP4 \/ MOV \/ AVI \/ MKV · 上限 (.+)$/,m=>`MP4 / MOV / AVI / MKV · 上限 ${m[1]}`],
      [/^此文件为 (.+)，超过 (.+) 的上限。$/,m=>`ファイルは ${m[1]} で、上限 ${m[2]} を超えています。`],
      [/^本次往返 (\d+) ms$/,m=>`今回の往復時間 ${m[1]} ms`],[/^任务 (.+)$/,m=>`タスク ${m[1]}`],
      [/^共 (.+) 帧$/,m=>`合計 ${m[1]} フレーム`],[/^关节 (\d+)$/,m=>`キーポイント ${m[1]}`],
      [/^身体 (\d+) · 面部关键点 (\d+) · 面部表情 (\d+)$/,m=>`身体 ${m[1]} · 顔ランドマーク ${m[2]} · 顔表情 ${m[3]}`],
      [/^(.+) · 未就绪$/,m=>`${translate(m[1],language)} · 未準備`],
      [/^(身体骨骼|面部关键点|面部表情) · (.+)$/,m=>`${({'身体骨骼':'身体骨格','面部关键点':'顔ランドマーク','面部表情':'顔表情'})[m[1]]} · ${translate(m[2],language)}`]
    ];
    for(const [pattern,render] of patterns){const match=text.match(pattern);if(match)return render(match);}
    return text;
  }
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
    const elements = Object.fromEntries(['loginPanel','loginForm','password','loginButton','loginMessage','application','logoutButton','connection','connectionText','model','modelNote','detector','detectorNote','videoTab','cameraTab','videoFields','cameraFields','videoFile','fileName','fileLimit','cameraPreviewButton','cameraHint','cameraWarning','threshold','thresholdValue','resetRoi','roiLabel','startButton','stopButton','progressPanel','progressLabel','progressValue','progress','message','downloads','downloadJson','downloadCsv','downloadVideo','media','preview','previewCanvas','placeholder','previewTitle','previewHint','frameSize','liveDot','jobStatus','jobDetail','processed','frameDetail','inferenceFps','latencyDetail','chartCanvas','chartEmpty','joint','jointReadout','language','modelCount','trajectoryPanel','trajectoryTitle','expressionPanel','expressionLabel','expressionScores','expressionHint'].map(id => [id, byId(id)]));
    const renderedText = new Map();
    let language = 'zh';
    try { language = window.localStorage?.getItem('monkeypose-language') === 'ja' ? 'ja' : 'zh'; } catch (_) { /* Storage can be disabled by the browser. */ }
    const t = text => translate(text, language);
    function setText(node, text) { if(!node)return; renderedText.set(node, String(text ?? '')); node.textContent=t(text); }
    const api = new RemoteAPI(window.fetch.bind(window));
    const state = {authenticated:false, models:[], limits:{}, kind:'video', file:null, objectURL:null, stream:null, job:null, busy:false, epoch:0, authEpoch:0, upload:null, pollTimer:null, frame:null, roi:[0,0,1,1], drag:null, samples:[], names:[], previewPending:false, resultPending:false, width:0,height:0, expression:null, language};
    const isActive = () => state.busy || Boolean(state.job && !TERMINAL.has(state.job.status));
    const notify = (text, error = false, node = elements.message) => {
      setText(node,text); node.dataset.error = String(error); node.classList.toggle('hidden', !text);
    };
    const connection = (text, mode = 'ready') => { setText(elements.connectionText,text); elements.connection.dataset.mode = mode; };
    const csrf = response => { if (response.csrf) api.csrf = response.csrf; };
    const currentModel = () => state.models.find(model => model.id === elements.model.value);
    const currentTask = () => currentModel()?.task || 'body_pose';
    const faceLabels={primateface_landmarks:'PrimateFace (68点)',primateface_expression:'猕猴面部表情CNN'};
    const modelField = (model, field) => model[`${field}_${language}`] || model[`${field}_zh`] || (field==='label' ? faceLabels[model.id] : '') || model[field] || '';
    const categoryLabel = model => ({body_pose:'身体骨骼',face_landmarks:'面部关键点',facial_expression:'面部表情'})[model.task || 'body_pose'] || '身体骨骼';
    function modelOptionText(model) { return `${categoryLabel(model)} · ${modelField(model,'label') || model.id}${model.ready ? '' : ' · 未就绪'}`; }
    function renderModelLabels() {
      for(const option of elements.model.options){const model=state.models.find(item=>item.id===option.value);if(model)setText(option,modelOptionText(model));}
      const count=task=>state.models.filter(model=>(model.task || 'body_pose')===task).length;
      setText(elements.modelCount,`身体 ${count('body_pose')} · 面部关键点 ${count('face_landmarks')} · 面部表情 ${count('facial_expression')}`);
    }
    function setLanguage(value) {
      language=value==='ja'?'ja':'zh';state.language=language;elements.language.value=language;
      try { window.localStorage?.setItem('monkeypose-language',language); } catch (_) { /* Private mode may disable storage. */ }
      if(document.documentElement)document.documentElement.lang=language==='ja'?'ja':'zh-CN';
      if(document.querySelectorAll){
        for(const node of document.querySelectorAll('[data-i18n]'))if(!renderedText.has(node))setText(node,node.getAttribute('data-i18n'));
        for(const name of ['aria-label','placeholder'])for(const node of document.querySelectorAll(`[data-i18n-${name}]`))node.setAttribute(name,t(node.getAttribute(`data-i18n-${name}`)));
      }
      for(const [node,text] of renderedText)node.textContent=t(text);
      renderModelLabels();modelChanged();roiLabel();renderExpression();drawChart();
    }
    function updateControls() {
      const active = isActive(), model = currentModel();
      for (const element of [elements.model,elements.threshold,elements.videoTab,elements.cameraTab,elements.videoFile,elements.resetRoi]) element.disabled = active;
      elements.detector.disabled = active || !model || !model.detector_supported || model.detector_ready === false;
      if (model && (!model.detector_supported || model.detector_ready === false)) elements.detector.checked = false;
      elements.startButton.disabled = !state.authenticated || active || !model || !model.ready || (state.kind === 'video' && !state.file) || (state.kind === 'camera' && !window.isSecureContext);
      elements.stopButton.disabled = !(active || state.stream);
      elements.cameraPreviewButton.disabled = active || !window.isSecureContext;
      elements.threshold.disabled = active || currentTask()==='facial_expression';
      elements.trajectoryPanel.classList.toggle('hidden',currentTask()==='facial_expression');
      elements.expressionPanel.classList.toggle('hidden',currentTask()!=='facial_expression');
      setText(elements.trajectoryTitle,currentTask()==='face_landmarks'?'面部关键点轨迹':'关节轨迹');
      elements.preview.dataset.selectable = String(!active && Boolean(state.frame));
      setText(elements.previewHint,active ? (state.kind === 'camera' ? '显示与预测匹配的画面；停止后释放摄像头' : '逐帧分析 · 画面为最近完成的预测') : '开始前可在画面上拖动，限定单猴区域');
      elements.liveDot.dataset.live = String(active && state.kind === 'camera');
    }
    function modelChanged() {
      const model = currentModel();
      setText(elements.modelNote,model ? (model.ready ? (modelField(model,'caveat') || '模型已在工作站就绪；结果保留原生关节定义。') : '此模型暂未在工作站就绪，请联系管理员。') : '工作站上尚无可用模型。');
      const face=currentTask()!=='body_pose';
      setText(byId('detectorLabel'),face?'自动定位面部':'自动定位猴子');
      setText(elements.detectorNote,face ? '面部模型自行定位面部，建议将 ROI 限定到单猴头部。' : (model && model.detector_supported ? (model.detector_ready === false ? '工作站检测器尚未就绪，请使用手动 ROI。' : '在所选 ROI 内自动定位单猴；使用工作站配置的权重。') : '此模型使用手动 ROI，自动检测不可用。'));
      updateControls();
    }
    function setModels(models) {
      const previous = elements.model.value;
      state.models = Array.isArray(models) ? models : Object.values(models || {});
      elements.model.replaceChildren();
      for (const model of state.models) {
        const option = document.createElement('option'); option.value = model.id;
        setText(option,modelOptionText(model));
        option.disabled = !model.ready; elements.model.append(option);
      }
      elements.model.value = state.models.some(model => model.id === previous && model.ready) ? previous : (state.models.find(model => model.ready)?.id || state.models[0]?.id || '');
      renderModelLabels();
      modelChanged();
    }
    function clearCamera() {
      camera.stop();
      if (state.stream) for (const track of state.stream.getTracks()) track.stop();
      state.stream = null; elements.media.pause(); elements.media.srcObject = null;
      setText(elements.cameraPreviewButton,'预览当前电脑的摄像头');
      setText(elements.cameraHint,'由浏览器申请摄像头权限，使用你正在操作的电脑上的摄像头。');
      updateControls();
    }
    function clearPolling() { if (state.pollTimer) window.clearTimeout(state.pollTimer); state.pollTimer = null; }
    function clearFrame() {
      if (state.frame && state.frame.image && typeof state.frame.image.close === 'function') state.frame.image.close();
      state.frame = null; state.width = 0; state.height = 0; elements.placeholder.classList.remove('hidden'); drawPreview();
    }
    function resetJobDisplay() {
      state.samples = []; state.names = []; state.expression=null; elements.joint.replaceChildren();renderExpression();
      const option = document.createElement('option'); option.value = '0'; setText(option,'等待关节定义'); elements.joint.append(option);
      elements.downloads.classList.add('hidden'); elements.progressPanel.classList.add('hidden');
      setText(elements.processed,'0'); setText(elements.inferenceFps,'—'); setText(elements.jobStatus,'待开始'); setText(elements.jobDetail,'尚未创建任务');
      drawChart();
    }
    function unauthenticated() {
      state.authEpoch++; state.epoch++; state.authenticated = false; state.busy = false;
      if (state.upload) state.upload.abort(); state.upload = null;
      clearPolling(); clearCamera(); state.job = null; api.csrf = '';
      clearFrame(); resetJobDisplay(); state.file=null; elements.videoFile.value=''; setText(elements.fileName,'选择视频文件');
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
      setModels(response.models); setText(elements.fileLimit,`MP4 / MOV / AVI / MKV · 上限 ${formatBytes(Number(state.limits.max_upload_bytes) || DEFAULT_MAX)}`);
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
      setText(elements.roiLabel,x1 === 0 && y1 === 0 && x2 === 1 && y2 === 1 ? 'ROI · 全画面' : `ROI · ${Math.round((x2-x1)*100)}% × ${Math.round((y2-y1)*100)}%`);
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
        const box=result.face_bbox_xyxy;
        if(Array.isArray(box)&&box.length===4&&box.every(finite)&&box[2]>box[0]&&box[3]>box[1]){
          const a=toScreen(box.slice(0,2)),b=toScreen(box.slice(2));ctx.strokeStyle='#edc986';ctx.lineWidth=1.4;ctx.strokeRect(a[0],a[1],b[0]-a[0],b[1]-a[1]);
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
      elements.placeholder.classList.add('hidden');setText(elements.frameSize,`${width} × ${height}`);drawPreview();updateControls();
    }
    function renderExpression() {
      const expression=state.expression;
      setText(elements.expressionLabel,!expression?'等待面部画面':expression.available && expression.label ? (CHINESE_CLASSES[expression.label] || expression.label) : '未检测到可用面部');
      elements.expressionScores.replaceChildren();
      if(!expression || !expression.available)return;
      const scores=Array.isArray(expression.probabilities)?expression.probabilities:[];
      for(const item of scores){
        if(!item || typeof item.label!=='string' || !finite(item.score) || item.score<0 || item.score>1)continue;
        const row=document.createElement('div');row.className='expression-score';
        const label=document.createElement('span');label.textContent=t(CHINESE_CLASSES[item.label] || item.label);
        const meter=document.createElement('progress');meter.max=1;meter.value=item.score;meter.setAttribute('aria-label',`${label.textContent} · ${t('模型分数（未经校准）')}`);
        const score=document.createElement('span');score.textContent=`${(item.score*100).toFixed(1)}%`;
        row.append(label);row.append(meter);row.append(score);elements.expressionScores.append(row);
      }
    }
    function addSamples(payload, fallback={}) {
      if(payload.expression){state.expression=payload.expression;renderExpression();}
      const trajectory = payload.trajectories || payload.trajectory || payload;
      const entries = Array.isArray(trajectory) ? trajectory : trajectory.samples || [];
      if(entries.length && entries[entries.length-1]?.expression){state.expression=entries[entries.length-1].expression;renderExpression();}
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
        names.forEach((name,index)=>{const option=document.createElement('option');option.value=String(index);setText(option,`${index+1} · ${name}`);elements.joint.append(option);});
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
      [15,10,5,0].forEach(seconds=>{const px=x(end-seconds);ctx.fillText(seconds===0?t('现在'):`−${seconds}s`,px-8,height-5);});
      for(const [key,color] of [['x','#3d7752'],['y','#b78b42']]){
        ctx.strokeStyle=color;ctx.lineWidth=1.65;ctx.beginPath();
        for(const [a,b] of segments){ctx.moveTo(x(a.time),padding.top+(1-a[key])*plotHeight);ctx.lineTo(x(b.time),padding.top+(1-b[key])*plotHeight);}ctx.stroke();
      }
      const latest=samples[samples.length-1],point=latest.keypoints_xy_score[joint];
      setText(elements.jointReadout,validPoint(point,joint,latest,Number(elements.threshold.value))?`X ${(point[0]/latest.width).toFixed(3)} · Y ${(point[1]/latest.height).toFixed(3)}`:'低分、缺失或超过 1 秒时断开');
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
        setText(elements.latencyDetail,`本次往返 ${Math.round(latency)} ms`);
        setText(elements.processed,String(result.processed??result.sequence??0));
        setText(elements.inferenceFps,finite(result.inference_fps)?result.inference_fps.toFixed(1):(1000/Math.max(1,latency)).toFixed(1));
        setText(elements.frameDetail,'串行发送 · 同时最多一帧');
        setText(elements.jobStatus,'分析中');
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
      setText(elements.cameraPreviewButton,'摄像头已就绪');setText(elements.cameraHint,'预览已开启。点击“开始分析”后才向工作站发送画面。');
      updateControls();return stream;
    }
    function progress(label,value,text) {
      elements.progressPanel.classList.remove('hidden');setText(elements.progressLabel,label);
      if(value===null){elements.progress.removeAttribute('value');setText(elements.progressValue,text||'…');}
      else{elements.progress.value=clamp(value,0,1);setText(elements.progressValue,text||`${Math.round(value*100)}%`);}
    }
    function applyJob(payload) {
      const job=jobObject(payload);if(!job||!state.job||job.id!==state.job.id)return;
      state.job={...state.job,...job};
      const status=state.job.status;
      setText(elements.jobStatus,STATUS_LABELS[status]||'准备中');
      setText(elements.jobDetail,state.job.kind==='camera'?'浏览器摄像头 · 当前会话':(status==='queued'?'等待工作站中的前序任务':`任务 ${state.job.id.slice(0,8)}`));
      const processed=Number(job.processed??job.processed_frames??job.frames_processed??0),total=Number(job.total_frames??job.total??0);
      if(state.job.kind!=='camera'){
        setText(elements.processed,String(processed));setText(elements.frameDetail,total?`共 ${total.toLocaleString()} 帧`:'逐帧处理上传的视频');
        if(finite(job.inference_fps))setText(elements.inferenceFps,job.inference_fps.toFixed(1));
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
        state.job.status='stopping';updateControls();setText(elements.jobStatus,'停止中');
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
      setText(elements.previewTitle,kind==='video'?'上传视频 · 分析画面':'浏览器摄像头 · 分析画面');
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
    elements.language.addEventListener('change',()=>setLanguage(elements.language.value));
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
    elements.threshold.addEventListener('input',()=>{setText(elements.thresholdValue,Number(elements.threshold.value).toFixed(2));drawChart();drawPreview();});
    elements.videoFile.addEventListener('change',()=>{
      if(isActive())return;
      state.epoch++;state.file=elements.videoFile.files[0]||null;state.job=null;state.roi=[0,0,1,1];roiLabel();resetJobDisplay();clearFrame();notify('');
      setText(elements.fileName,state.file?state.file.name:'选择视频文件');
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
    setLanguage(language);
    session().catch(error=>{unauthenticated();notify(error.message||'暂时无法连接工作站，请检查网络后刷新。',true,elements.loginMessage);connection('连接未完成','error');});
    return {state,api,startJob,stopJob,session,setLanguage};
  }
  return {RemoteAPI,APIError,CameraRunner,uploadVideo,jobPath,formatBytes,jpegBlob,containedRect,pointInFrame,validPoint,normalizeSample,segmentsForJoint,TERMINAL,translate,mount};
});
