# 模型后端配置

桌面服务通过JSON和临时JPEG与独立模型Python进程通信，不要求所有深度学习库安装在同一个环境。每个后端都应先按作者说明建立可用环境，再在应用中填写路径；本软件不自动执行下载的安装脚本或下载模型权重。

| 标识 | 已适配的作者版本 | 来源与许可 |
|---|---|---|
| `mmpose` | MMPose 0.29.0，MacaquePose HRNet-W48 | [OpenMMLab](https://github.com/open-mmlab/mmpose/tree/v0.29.0)，Apache-2.0 |
| `macaque3dpose_2d` | `f1da814cf369987d3e2b538e41a9f604c73c7784` | [macaque3Dpose](https://github.com/PrimatoModelling/macaque3Dpose/tree/f1da814cf369987d3e2b538e41a9f604c73c7784)，MIT；只适配其2D前端 |
| `vitpose_animal` | `c050ed29112da7704797cc1a65af0234b525010d` | [ViTPose](https://github.com/ViTAE-Transformer/ViTPose/tree/c050ed29112da7704797cc1a65af0234b525010d)，Apache-2.0 |
| `xpose` | `e8dae4b5cbe6c8dc185e4905582aa6c904f817f1` | [X-Pose](https://github.com/IDEA-Research/X-Pose/tree/e8dae4b5cbe6c8dc185e4905582aa6c904f817f1)，[非商业科研许可](https://github.com/IDEA-Research/X-Pose/blob/e8dae4b5cbe6c8dc185e4905582aa6c904f817f1/LICENSE.txt) |
| `dlc` | DeepLabCut TensorFlow后端，TensorFlow 2.15系列 | [DeepLabCut](https://github.com/DeepLabCut/DeepLabCut)，[LGPL-3.0](https://github.com/DeepLabCut/DeepLabCut/blob/main/LICENSE)；用户提供训练项目和检查点 |

本仓库提供的是适配层，模型作者代码及权重没有被重新许可。数据集许可、模型权重许可和框架代码许可应分别核对。X-Pose的原许可对用途有明确限制，使用者需遵守原条款。

## 常用配置字段

参考根目录 `config.example.json`。路径可以为绝对路径，或相对本地配置文件所在目录的路径。

- 所有模型：`python`、`config`、`checkpoint`。
- macaque3Dpose、ViTPose、X-Pose：作者源码目录 `repo`。
- X-Pose：CLIP权重 `clip_checkpoint`。
- DLC：原项目 `config`、测试姿态配置 `pose_config`、不含后缀的TensorFlow检查点前缀 `checkpoint`。需要完整`.index`和`.data-*`等文件；17点定义应与适配器相符。
- 可选完整性记录：`checkpoint_sha256`，DLC可提供`checkpoint_files_sha256`。
- 可选YOLO：`detector.checkpoint`。检测类别0应是目标猴；该环境需要安装兼容的Ultralytics。

路径存在只代表配置准备就绪，不能证明依赖版本、CUDA、检查点结构或解剖点位正确。实际加载时保留模型头结构、原生点名和完整性检查；遇到不兼容应修正环境／配置，不能用随机初始化或伪造坐标替代。

MMPose旧API与新版本不同，应使用适配版本。ViTPose按原作者多数据集检查点选择AP10K分支；X-Pose保留对象分数语义；DLC后端当前使用CPU与手动ROI。更换检查点或点位定义时需要重新验证兼容性。

