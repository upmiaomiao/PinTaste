# 三台Windows worker部署

每台电脑解压到自己的固定工作目录，各自创建Python环境和密钥。不复制`.venv`、账户`.env`、`.worker.env`、`cluster.json`或影像文件。向日葵用于远程维护；API的数据通道是局域网HTTP或各节点自己的受鉴权通道，不经过向日葵。

## 每台电脑的首次准备

先安装64位Python 3.11或更新版本（实测3.13），安装并登录本机奥维。确认当前账户实际能看到需要的影像，在奥维系统设置启用第三方WebSocket，本例端口7699；使用本机自己的PSK，9.8.5建议不超过16字符。

在项目根目录PowerShell运行：

```powershell
.\scripts\deploy\setup-worker.ps1 -PromptForOmapPsk
.\scripts\deploy\start-worker.ps1
```

若默认Python指向错误位置，可加 `-PythonExecutable 'C:\Python313\python.exe'`。setup会保留已经生成的服务密钥，不重复轮换。PSK输入不回显；生成的worker服务密钥仅写入根目录私有`.worker.env`，ACL只允许当前用户及SYSTEM。服务密钥用于gateway到worker的Bearer鉴权，与奥维PSK不同。不要把真实密钥粘贴进命令行、截图、日志或源码。

自定义图源ID是每台电脑的本地配置。先核对本机 `/api/maps`，把需要覆盖的ID写入自己的JSON文件，再运行：

```powershell
.\scripts\deploy\setup-worker.ps1 -SourceMapTypesFile '.\my-source-map-types.json'
```

`source-map-types.example.json`仅是此项目最初电脑的示例，不能当成其他电脑已经验证的结果。ArcGIS独立REST来源不需要本机MapType。空`OMAP_PSK`允许应用读取本机既有`.env`；两处均未配置时科学数据接口仍可用，奥维光学接口不能取图。更改worker配置后应先停止再启动，不会给运行中的进程静默换密钥。

```powershell
.\scripts\deploy\stop-worker.ps1
.\scripts\deploy\start-worker.ps1
```

start-worker会拒绝复用未经核验的旧无鉴权API。若此前用`start-api.ps1`启动了旧服务，先运行根目录`stop-api.ps1`再启动worker；脚本不会自动停止未知服务，也不会停止奥维。

## 同一局域网与异地电脑

默认仅监听`127.0.0.1:8765`。同一局域网由操作者显式指定这台电脑的实际IPv4：

```powershell
.\scripts\deploy\start-worker.ps1 -ListenHost '192.168.1.21' -Port 8765
```

脚本拒绝`0.0.0.0`、DNS名称和隐式开放全部网卡；监听IP必须属于这台电脑。Windows防火墙访问范围由管理员按实际局域网配置。异地电脑保持localhost监听，各自建立ngrok等受鉴权通道；本部署脚本不安装、不登录、不发布ngrok。

gateway配置放在它所在电脑的私有`cluster.json`，从根目录`cluster.example.json`创建。每个节点填写自己的`id`、`base_url`、服务`api_key`、实际可用`optical_sources`和`science_slots`；外部客户端使用单独的clients密钥。复制服务密钥到私有cluster文件应由操作者在本机安全完成，不把key放进公网URL。gateway默认监听`127.0.0.1:8766`，对外仅发布统一gateway；完成任务后先从生成节点取回结果文件，再缓存于gateway自己的目录，后续下载不依赖该节点继续在线。

gateway使用根目录`setup-gateway.ps1`、`start-gateway.ps1`、`stop-gateway.ps1`。启动脚本通过`omap_api.gateway:load_gateway --factory`在新进程内读取私有配置；导入源码不会提前载入账户配置。ngrok安装/账户配置/启动/停止分别使用根目录`install-ngrok.ps1`、`setup-ngrok.ps1`、`start-ngrok.ps1`、`stop-ngrok.ps1`，按`CLOUD_DEPLOYMENT.md`操作。部署ZIP只包含这些公开脚本，不含实际`ngrok.yml`、账户token或`tools`中的ngrok可执行文件；目标电脑自行从官方渠道安装。

奥维单个桌面会话每台只做1个光学任务；科学任务每台最多2个。排队、状态和轮询由gateway负责。三台电脑和奥维必须持续运行，关闭自动休眠、确保电源/网络稳定；锁屏或账户注销后是否能持续取图应按实际奥维会话测试。配置新图源或变更本机MapType后再次核验。

## 构建给其他电脑的包

```powershell
.\scripts\deploy\build-deployment-package.ps1 -Destination 'D:\Delivery\omap-workers.zip'
```

默认ZIP含Python源码、依赖清单、根启动停止脚本、部署脚本和文档模板；目标电脑联网运行setup安装依赖，不含已安装虚拟环境。已有ZIP不会覆盖。若需要离线安装：

```powershell
.\scripts\deploy\build-deployment-package.ps1 -Destination 'D:\Delivery\omap-workers-offline.zip' -IncludeWheels
# 在解压后的目标电脑运行，Python版本/架构应与打包电脑一致。
.\scripts\deploy\setup-worker.ps1 -OfflineWheels -PromptForOmapPsk
```

构建使用文件白名单及压缩包二次检查，排除私有配置、输出、缓存、日志、密钥文件和大影像。每台电脑通过setup生成独立随机服务密钥。SHA256用于核对ZIP传输完整性；它不是服务密钥。
