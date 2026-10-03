# 印前色彩软打样台（Prepress Color Soft-Proofing Station）

本地运行的 Web 应用：上传 **RGB / CMYK 的 TIFF、PNG 或 JPEG** 图像，选择**源 ICC 配置、印刷 ICC 配置、显示 ICC 配置**与渲染意图，由后端通过 **LittleCMS 2（直接 ctypes 调用 + Pillow ImageCms）** 完成全链路色彩转换，并在浏览器中进行显示器软打样、像素级 **CIEDE2000 色差**分析与区域取色。源色与印后色以 LittleCMS 原生 `TYPE_Lab_DBL`（C double）高精度 Lab 取得，不再经过 Pillow 8 位 `LAB` 模式的额外量化。

此外内置 **印刷配置版本发布与回归门禁** 模块：把同一配置族的候选印刷 ICC 存为按 SHA-256 固定的草稿，从已保存的代表性任务固化门禁基线，服务按草稿快照自动重跑并逐项给出 ΔE 均值 / P95 / 超阈面积比例 / 遮罩变化像素的通过-失败判定与变化像素定位；**全部通过才能在事务中发布为当前版本**，发布、撤回、恢复（回滚）均带期望版本号并发控制并写不可变哈希链审计。

- 后端：Python 3.12 · FastAPI · SQLite · liblcms2 直接 ctypes（高精度 Lab）+ Pillow（图像 I/O/ImageCms）· NumPy
- 前端：原生 HTML / CSS / JavaScript（无框架、无构建步骤）
- 部署：Docker Compose 一键启动

---

## 一键启动

需要安装 Docker（含 Compose v2）。

```bash
docker compose up -d --build
```

启动后浏览器访问：

> **http://localhost:8000**

页面顶部导航可在三个视图间切换：

- **软打样**：上传图像、选择配置/意图/阈值，生成软打样与 ΔE 分析；
- **局部 CMYK 修色**：从已保存任务框选 ≤64×64 选区，搜索统一整数 CMYK 偏移并导出嵌入印刷 ICC 的修色 TIFF；
- **印刷配置版本发布与回归门禁**：配置族、草稿、基线、门禁结果、版本发布/撤回/回滚与审计。

局部修色的操作与查看路径：先在“软打样”页上传图像并生成一个已保存任务 → 打开“**局部 CMYK 修色**”页（http://localhost:8000 顶部导航第二项）→ 在“选择已保存任务”下拉选中该任务 → 在软打样图上拖拽框选（或手填 x/y/宽/高）→ 填写目标 Lab、容差、偏移上限、总墨量上限后点击“**搜索统一 CMYK 偏移**”。结果面板直接展示可达/不可达裁决、可行偏移数、修色前/后平均 Lab 与对目标 ΔE00、最优偏移与修色前/后选区预览；面板内可**下载修色 TIFF**（仅可达时）与**修色报告 JSON**，展开“查看修色报告”可阅读完整报告。也可直接访问：

- 修色报告：`GET /api/retouch/<retouch_id>/report`（页面“下载修色报告 JSON”即是此 URL）；
- 选区预览：`/api/retouch/<retouch_id>/before.png`、`/after.png`；
- 成品：`/api/retouch/<retouch_id>/retouched.tif`（不可达返回 `422`）。

对**同一任务**先后提交**不同矩形或不同约束**会得到不同 `retouch_id` 与各自独立的搜索、报告、预览及 TIFF（即使两组坐标无分隔拼接后恰好同形，如 `(1,23,4,5)` 与 `(12,3,4,5)`）；**完全相同的请求**重复提交则复用同一身份、结果一致。

停止：

```bash
docker compose down
```

数据（上传的图像任务、ICC 配置、局部修色产物、草稿/版本快照、门禁基线与产物、SQLite 数据库）保存在 Docker 卷 `softproof-data`（容器内 `/data`）中，容器重启后仍然存在。

### 不使用 Docker（本地开发）

```bash
pip install -r requirements.txt
# 还需要系统安装 LittleCMS 2 共享库，例如 Debian/Ubuntu：
# sudo apt-get install liblcms2-2
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 功能与色彩流程

### 处理链路

对每一张上传图像，后端依次执行（全部由 LittleCMS 完成转换，**绝不使用浏览器默认色彩解释**）：

1. **源图显示预览**：源像素 `源配置 → 显示配置`；
2. **沿 源 → 印刷 → Lab 回算色值**（Lab 均为 LittleCMS 原生 **TYPE_Lab_DBL 双精度**结果，不经过 8 位 PCS 量化）：
   - 源像素 `源配置 → 印刷配置` 得到设备 **CMYK** 值；
   - CMYK `印刷配置 → Lab PCS（double）` 得到印刷后 Lab；
   - 同时源像素 `源配置 → Lab PCS（double）` 得到原始 Lab；
3. **软打样图**：印刷后高精度 Lab `→ 显示配置`（可选择显示渲染意图，仍按所选显示 ICC 呈现）；
4. 对 `Lab_源` 与 `Lab_印刷后`（同一份双精度 Lab 结果）逐像素计算 **CIEDE2000（ΔE00）**；
5. 生成：
   - **ΔE 热图**（黑→蓝→青→绿→黄→红伪彩色）；
   - **热图叠加图**（半透明叠加在软打样上）；
   - **超阈值二值遮罩**（ΔE00 ≥ 阈值的像素为白色）；
   - 均值 / P95 / 最大 ΔE00 与**超阈值面积比例**统计。

**热图、遮罩、统计、框选区域的 Lab/色差值与导出报告全部取自同一份双精度 Lab/ΔE 结果**——阈值设在像素实际 ΔE00 附近时，不会因为源色/印后色被先写成 Pillow 8 位 LAB 再解码而错误翻转临界像素的遮罩归属或超阈面积比例。

输出给浏览器的 PNG 均嵌入 ICC 配置，浏览器不会按未标记数据自行解释：**源图显示预览与软打样嵌入所选择的显示配置**（像素本身位于该显示空间）；**ΔE 热图、遮罩与叠加图是无物理色度含义的合成可视化，嵌入 sRGB** 以保证颜色稳定呈现。

### 渲染意图

源→印刷、Lab→显示两个阶段分别可选：

- 感知（Perceptual）
- **相对色度（Relative Colorimetric，默认）**
- 饱和度（Saturation）
- 绝对色度（Absolute Colorimetric）

### 区域框选

在任一预览图上**鼠标拖拽框选矩形区域**，右侧面板显示该区域：

- 源 Lab 与印刷后 Lab 的均值/最小/最大；
- 印刷 **CMYK（C,M,Y,K 百分比，0% 无墨 / 100% 满版）**均值与最大；
- 区域 ΔE00 均值 / P95 / 最大 / 超阈值面积比例；
- 5×5 采样网格的各阶段数值。

### 导出

每个任务可导出（单个或一键 ZIP 打包）：

- 源图显示预览 PNG、软打样 PNG、ΔE 热图 PNG、热图叠加 PNG、超阈值遮罩 PNG；
- `report.json`：**绑定原图 SHA-256、三份配置的名称/类/色彩空间/SHA-256 摘要、渲染意图与阈值参数、统计结果**。

### 确定性（重复输入结果一致）

任务 ID = 原图哈希 + 三份配置哈希 + 两个渲染意图 + 阈值 的 SHA-256。相同输入会命中同一任务目录与同一报告；报告不含时间戳，ZIP 条目时间固定为 1980-01-01，因此**重复输入产生字节级一致的结果**。

---

## 局部 CMYK 修色（Local CMYK Retouch）

在不重新分色的前提下，对已保存任务的一小块区域做**统一整数 CMYK 偏移**修色。进入顶部导航的 **局部 CMYK 修色** 视图：

1. 从“**选择已保存任务**”下拉中挑选一个已经在“软打样”页跑过的任务（页面载入该任务的软打样图）；
2. 在图上**鼠标拖拽框选矩形选区**（也可手填 x/y/宽/高），**宽、高均不得超过 64 像素**；
3. 输入修色参数（后端逐项严格校验，越界一律 `422` 拒绝）：
   - **目标 Lab**：`L* ∈ [0, 100]`，`a*, b* ∈ [-128, 127]`（有限数值）；
   - **ΔE00 容差**：非负数值，选区平均 Lab 与目标的 CIEDE2000 色差不超过它才算达标；
   - **单通道偏移绝对值上限**：`0..8` 的整数，每个通道的 `|dC|、|dM|、|dY|、|dK|` 都不得超过；
   - **总墨量上限**：`0..1020`（=4×255）的整数，修色后选区每个像素的 `C+M+Y+K` 都不得超过；
4. 点击“**搜索统一 CMYK 偏移**”。

### 搜索与选优规则

- 以**原任务分色结果 `cmyk.npy` 为唯一起点**，按**原任务的印刷 ICC**（与原任务 `source→press` 同一渲染意图）枚举所有对选区统一生效的整数偏移四元组 `(dC,dM,dY,dK)`；
- 一组偏移可行，当且仅当它对选区**每个像素**都满足：通道值保持 `0..255`、各通道偏移绝对值不超过上限、总墨量不超过上限；
- 用 **LittleCMS `TYPE_Lab_DBL` 回算**偏移后选区的**像素加权平均 Lab**，并计算其到目标 Lab 的 ΔE00；相同 CMYK 四元组在候选间只回算一次；
- 按以下顺序选最优：**① 平均 Lab ΔE00 最小 → ② 偏移绝对值之和 `|dC|+|dM|+|dY|+|dK|` 最小 → ③ `(C,M,Y,K)` 字典序最小**；
- 若不存在任何可行偏移，或最优 ΔE00 仍大于容差，页面标记 **不可达**：显示修色前后软打样与残余色差、**不生成成品 TIFF**，请求 TIFF 端点返回 `422`。

### 页面展示与导出

- 选区**修色前 / 修色后软打样** PNG（嵌入原任务所选显示 ICC）与**残余色差**：修色前后平均 Lab、各自到目标的 ΔE00，以及达标时选区内逐像素 ΔE00 的均值/最大值；
- 达标时可下载 **`retouched.tif`**：整幅 8 位 CMYK TIFF（无压缩、嵌入**原印刷 ICC**），**选区外所有 CMYK 字节与原任务分色结果逐字节相同**，选区内统一加上最优偏移；
- **修色报告 JSON**：记录原任务 ID/原图哈希/尺寸/意图、印刷与显示 ICC 的名称/类/色彩空间/SHA-256 摘要、选区矩形与像素数、全部约束（目标 Lab、容差、偏移上限、总墨量上限）、可行偏移数量、选优规则、最终偏移与残余色差、以及成品文件名；
- 修色产物按身份的 SHA-256 内容寻址存于 `/data/retouch/<retouch_id>/`，重复请求复用同一结果，报告不含时间戳。

#### 请求身份（retouch_id）与缓存复用规则

`retouch_id = SHA-256(原任务 + 完整请求)`，身份串中的每个字段都采用**带长度前缀的自界定编码**，并覆盖：原任务 ID、原任务的两个渲染意图、选区 `x/y/width/height`、目标 Lab 三个分量、ΔE00 容差、单通道偏移上限、总墨量上限。规则保证：

- **不同请求必然区分**：坐标等整数字段不再无分隔拼接——例如同一任务先后提交 `(x,y,w,h)=(1,23,4,5)` 与 `(12,3,4,5)`（旧实现两者都拼出 `"12345"` 而串用结果）现在得到两个不同身份、两份独立的搜索/报告/预览/成品；
- **目标 Lab 与容差按完整双精度参与哈希**：使用浮点最短往返表示（Python/JSON `repr`），不再做六位小数格式化，相差 1e-7 的两个目标或容差不会被错误合并；`50` 与 `50.0` 等语义相同的写法身份一致；校验阶段已拒绝 NaN/±∞，`-0.0` 归一为 `0.0`；
- **相同请求稳定复用**：逐字段相同的重复提交命中同一目录，搜索、报告、预览与 TIFF 字节级一致。

**命中缓存时仍逐项重新核对绑定**，只有全部通过才复用，否则删除/忽略旧产物并按本次请求重新计算：

1. 报告的 `retouch_id`、原任务（ID、原图 SHA-256、尺寸、两个渲染意图）与本次一致；
2. 报告记录的印刷/显示 ICC SHA-256 与当前读取并校验的 ICC 字节一致（印刷 ICC 必须与任务固化摘要一致，配置被替换时在更早阶段即 `422` 拒绝）；
3. 报告选区矩形（按本次请求与原任务图像尺寸重新裁剪）、像素数与本次请求一致；
4. 约束块（目标 Lab、容差、偏移上限、总墨量上限）与本次请求一致；可达/不可达裁决、可行偏移数、最终偏移与残余色差自洽且满足本次约束；
5. 报告记录的各产物 SHA-256（`artifact_sha256`：两张预览 PNG，可达时另含 `retouched.tif`）与磁盘实际字节一致；预览 PNG 尺寸等于选区且嵌入显示 ICC；可达时 TIFF 可打开、为整幅 CMYK、嵌入原印刷 ICC、选区内差值恰为报告偏移并逐像素满足通道/总墨量约束、选区外字节与原任务分色逐字节相同；不可达时不得残留 TIFF。

产物下载端点同样按报告摘要校验字节，摘要不符返回 `422`。写盘采用“同目录临时文件 + 原子替换”，`report.json` 最后提交，避免崩溃或并发下读到半截结果。

### ICC 绑定（安全校验）

修色只接受**原任务所绑定的印刷 ICC 字节**：任务创建时固化印刷（及显示）ICC 的全 64 位 SHA-256 摘要，执行修色时重新读取当前文件并比对——**原任务印刷 ICC 缺失、无法打开或摘要不符（配置被替换/升级）一律 `422` 拒绝**，不会在错误的配置上搜索或出成品。需要在新配置下修色时，请用新配置重新跑软打样任务。

---

## 严格的配置/通道校验

以下情况后端一律返回 `422` 并拒绝处理（不会按浏览器默认色彩空间解释图像）：

| 情形 | 处理 |
| --- | --- |
| 未选择/找不到所需 ICC 配置 | 拒绝 |
| ICC 文件损坏、缺少 `acsp` 签名、lcms 无法打开 | 拒绝（上传配置时即拒绝） |
| 印刷配置不是 CMYK 输出配置（非 `prtr/spac` + `CMYK`） | 拒绝 |
| 显示配置不是 RGB 显示器配置（非 `mntr` + `RGB`） | 拒绝 |
| 图像通道数/色彩空间与源配置不匹配（例如 RGB 图配 CMYK 源） | 拒绝 |
| 勾选“使用内嵌配置”但图像无内嵌 ICC，或内嵌 ICC 无效 | 拒绝 |
| 16 位、索引色（P/LA/I 等）、GIF/WebP 等非支持格式 | 拒绝，请先在印前软件中转换并嵌入 ICC |
| RGBA 图 | 透明通道合成到白色后按 3 通道 RGB 处理（唯一的规范化操作） |
| 图像最长边超过上限（默认 3000px） | 拒绝 |

支持的输入容器：**TIFF / PNG / JPEG**；支持的图像色彩空间：**8 位 RGB、RGBA、CMYK、灰度 L**（灰度需搭配灰度源配置）。

---

## 内置与自定义 ICC 配置

首次启动时应用自动生成：

- `sRGB IEC61966-2.1 (built-in)`：默认可选的源 / 显示配置；
- `CIELAB identity (built-in)`：流水线内部使用的 Lab PCS 配置（ICC v2）；
- `Soft Proofing Demo SWOP-like CMYK (synthetic)`：**合成的、自包含的合法 CMYK 输出配置**，让开箱即可演示完整 RGB→CMYK→显示链路。其色域模型是简化模型（互补油墨 + sRGB 色度），**仅供演示，不可用于生产**。

生产使用时，在页面“**上传自定义 ICC 配置**”处上传真实的印刷机/打样机 CMYK 配置（如 GRACoL / SWOP / FOGRA `.icc/.icm`），或把文件放入数据目录（见下）。也可直接上传显示器校准后的 RGB 配置作为显示配置。

上传的配置按角色存放（容器内 `/data`）：

```
/data/profiles/source/    # 源（RGB/CMYK/灰度）
/data/profiles/press/     # 印刷（CMYK）
/data/profiles/display/   # 显示（RGB）
/data/profiles/builtin/   # 内置自动生成，勿手动修改
```

将 `.icc/.icm` 文件放进对应目录后刷新页面即可识别。

---

## 印刷配置版本发布与回归门禁

用于在更换/升级印刷 ICC 配置时，用固定的代表性任务做回归把关，防止配置改动造成大面积色差或超阈遮罩漂移。

### 概念

- **配置族（family）**：同一印刷机/工艺的一组草稿与已发布版本。族上有单调递增的 **族版本号 `revision`**，是发布/撤回/恢复操作的**期望版本号（乐观锁）**。
- **草稿（draft）**：上传到族上的候选印刷 ICC。服务校验其为合法 CMYK 输出配置后，按内容 **SHA-256** 存为不可变快照 `/data/release/snapshots/<sha256>.icc`；同一族再传新草稿会取代旧草稿的“开放”状态，但历史记录与快照保留。
- **基线（baseline）**：从一个**已保存的代表性任务**固化而来的自包含夹具。固定内容包括：
  - 原始图像字节（SHA-256 校验）；
  - 源 ICC（按任务的源配置解析，或图像内嵌 ICC）、显示 ICC（SHA-256 校验）；
  - 源→印刷、Lab→显示两个渲染意图与 ΔE 阈值；
  - 任务当时的二值超阈遮罩；
  - 四项允许上限：**ΔE 均值、ΔE P95、超阈面积比例（0..1）、遮罩变化像素数**。
- **门禁运行（gate run）**：服务**按草稿 ICC 的 SHA-256 取出固定快照**，替换基线任务的印刷配置，对族内**全部基线**重跑流水线，逐项返回 `passed/failed`、实测值、基线参考值、上限与中文原因；`run_id = SHA(草稿快照, 基线集合)`，**同输入重跑复用同一报告与产物**，因此历史报告可按版本复现。
- **版本发布**：只有全部门禁通过，才在**单个数据库事务**内完成：旧 `current → superseded`、草稿分配新版本号并置为 `current`、族 `current_version_id` 与 `revision` 前进、追加审计。失败则**保留草稿**、族当前版本不变。
- **撤回 / 恢复（回滚）**：可撤回当前版本（族暂时没有 current）；任何已发布的历史版本（被取代或已撤回）都可恢复为当前版本，恢复不需要重跑门禁——该版本发布时的门禁报告按 `gate_run_id` 仍可完整复现。两者都必须带期望版本号，过期返回 **409 Conflict**。
- **不可变审计**：族创建、草稿上传、基线固化、发布、撤回、恢复都会向 `audit_log` 追加一条带 `prev_hash/entry_hash` 的**哈希链**记录；表上建有 SQLite 触发器，任何 `UPDATE`/`DELETE` 都会被数据库直接拒绝。

### 页面操作流程

1. 在顶部进入 **印刷配置版本发布与回归门禁** 视图；
2. 左侧 **新建配置族**（如 “GRACoL2013-CRPC6”）；
3. 先到 **软打样** 视图跑一个代表性任务；回到发布视图，在 **① 门禁基线** 区从“最近已保存任务”下拉选中它，填写基线名称与四项允许上限，点击 **从所选任务固化基线**；
4. 在 **②** 区上传候选印刷 ICC 草稿（可打标签）；
5. 点击 **③ 运行回归门禁**：每个基线给出四项判定表、候选软打样/候选超阈遮罩/变化像素定位图（红=新增超阈、蓝=消失超阈、灰=一致）以及变化像素坐标 CSV 下载；
6. 全部通过后点 **④ 在事务中发布**（按钮显示当前族版本号作为期望版本号；失败时按钮禁用）；
7. 在 **版本与回滚** 表中可撤回当前版本、恢复（回滚）到任意历史版本、下载该版 ICC、打开发布时的门禁报告；左侧底部可展开查看当前族的哈希链审计日志。

### 进行中任务与历史版本的快照绑定

发布时，该版 ICC 以**不可变文件名** `release-<sha256前16位>.icc` 写入 `/data/profiles/press/` 并立即出现在软打样页的印刷配置下拉中；文件按内容寻址、**永不覆盖**。普通软打样任务的 `job_id` 由“图像哈希 + 三份配置哈希 + 意图 + 阈值”决定，因此：

- 发布新版本不会改动旧文件，**进行中/已存在的任务继续绑定原快照**，同输入永远命中原任务目录与报告；
- 新版本发布后，旧版本状态变为 `superseded` 但其 ICC 与门禁产物仍在，**历史报告可按版本（`gate_run_id`）字节级复现**；
- 回滚只切换数据库中的“当前版本”指针，不修改任何 ICC 文件。

### 模块数据目录（容器内 `/data/release`）

```
/data/release/
├── snapshots/<sha256>.icc          # 草稿/版本 ICC 不可变快照（内容寻址）
├── baselines/<baseline_id>/       # 基线自包含夹具
│   ├── image.bin                  #   固定的原始图像
│   ├── source.icc                 #   固定的源 ICC（内嵌源时为空）
│   ├── display.icc                #   固定的显示 ICC
│   ├── press-ref.icc              #   基线原印刷 ICC（参考复算用）
│   └── mask_ref.npy               #   基线二值超阈遮罩
└── gates/<run_id>/<baseline_id>/  # 门禁产物：softproof/mask/diff PNG、
                                   # changed_pixels.csv、mask_changed.npy、report.json
```

---

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/profiles` | 按 source/press/display 分组列出可用配置 |
| POST | `/api/profiles/{source\|press\|display}` | 上传并严格校验 ICC 配置（multipart `file`） |
| POST | `/api/jobs` | 上传图像并执行软打样（multipart 表单，见下） |
| GET | `/api/jobs` | 最近任务列表（SQLite） |
| GET | `/api/jobs/{id}` | 任务 JSON 报告 |
| GET | `/api/jobs/{id}/images/{name}.png` | preview / softproof / heatmap / overlay / mask |
| GET | `/api/jobs/{id}/report` | 下载 report.json |
| GET | `/api/jobs/{id}/export.zip` | 全部 PNG + JSON 打包下载 |
| POST | `/api/jobs/{id}/region` | 区域分析，body：`{x,y,width,height}` |
| POST | `/api/retouch` | 局部 CMYK 修色：以已保存任务为起点搜索统一整数偏移，body：`{job_id,x,y,width,height,target_lab:[L,a,b],tolerance_de,max_shift,tac_limit}`；身份按完整请求自界定编码，目标 Lab/容差保留全精度；命中缓存仍逐项核对任务/矩形/约束/ICC/产物绑定，不符即重算；不可达不产出 TIFF |
| GET | `/api/retouch/{rid}/report` | 修色报告 JSON（原任务、ICC 摘要、约束、偏移、残余色差、产物 SHA-256） |
| GET | `/api/retouch/{rid}/before.png` / `after.png` | 选区修色前 / 后软打样（嵌入显示 ICC）；字节与报告摘要不符返回 422 |
| GET | `/api/retouch/{rid}/retouched.tif` | 修色 CMYK TIFF（嵌入印刷 ICC，选区外字节不变）；不可达返回 422，字节与报告摘要不符返回 422 |

### 发布与回归门禁 API（前缀 `/api/release`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/families` | 配置族列表（含当前版本、开放草稿、版本/基线数、revision） |
| POST | `/families` | 新建配置族，body：`{name}` |
| GET | `/families/{fid}` | 族详情（全部版本、基线、最近门禁） |
| POST | `/families/{fid}/drafts` | 上传候选印刷 ICC 草稿（multipart `file` + 可选 `label`），严格 CMYK 校验，按 SHA-256 固定快照 |
| GET | `/versions/{vid}` | 版本/草稿详情（发布版本附带其门禁报告，可按版本复现） |
| GET | `/versions/{vid}/icc` | 下载该草稿/版本绑定的固定 ICC |
| GET | `/families/{fid}/baselines` | 列出基线 |
| POST | `/families/{fid}/baselines` | 从已保存任务固化基线，body：`{name, job_id, limit_de_mean, limit_de_p95, limit_over_ratio, limit_changed_pixels}` |
| POST | `/families/{fid}/gates` | 运行回归门禁，body：`{draft_version_id}`；逐项返回判定/实测/原因/定位 |
| GET | `/gates/{run_id}` | 门禁报告（确定性 run_id，可历史复现） |
| GET | `/gates/{run_id}/baselines/{bid}/softproof.png` | 候选软打样 |
| GET | `/gates/{run_id}/baselines/{bid}/mask.png` | 候选超阈遮罩 |
| GET | `/gates/{run_id}/baselines/{bid}/diff.png` | 变化像素定位图（红新增/蓝消失/灰一致） |
| GET | `/gates/{run_id}/baselines/{bid}/changed_pixels.csv` | 变化像素坐标清单（y,x,kind） |
| POST | `/families/{fid}/publish` | 事务内发布，body：`{draft_version_id, expected_revision, label?}`；门禁失败 422，期望版本号冲突 409 |
| POST | `/versions/{vid}/withdraw` | 撤回当前版本，body：`{expected_revision, reason?}` |
| POST | `/versions/{vid}/restore` | 恢复/回滚到历史版本，body：`{expected_revision, reason?}` |
| GET | `/audit?family_id=&limit=` | 哈希链不可变审计列表 |

错误码：请求语义/状态非法 `422`，资源不存在 `404`，期望版本号冲突 `409`。

`POST /api/jobs` 表单字段：

- `image`：图像文件；
- `source_profile` / `press_profile` / `display_profile`：配置 ID；
- `source_intent` / `proof_intent`：`perceptual | relative_colorimetric | saturation | absolute_colorimetric`；
- `threshold`：ΔE00 阈值（默认 3.0）；
- `use_embedded`：`true/false`，使用图像内嵌 ICC 作为源配置。

---

## 实现说明

- **色差公式**：`app/colorscience.py` 中为矢量化 CIEDE2000，已对照 Sharma 等 (2005) 标准测试数据验证（最大偏差 < 1e-3）。
- **Lab 精度**：源色与印后色由 `app/lcms_native.py` 直接 ctypes 调用 `liblcms2`，以 `cmsCreateTransform` + `TYPE_Lab_DBL`（native C double，D50 CIELAB）取回；ΔE00、热图、遮罩、统计、框选区域与 `report.json` 共用这同一份未量化结果，落盘的 Lab/ΔE 数组也以 float64 保存。仅保留 ICC v2 8 位 Lab 编解码函数用于与历史路径对比的测试。
- **合成 CMYK 配置**：`app/lcms_native.py` 通过 ctypes 调用系统 `liblcms2` 的 `cmsCreateProfilePlaceholder` / CLUT16 pipeline API 生成 17³/17⁴ 网格的合法 ICC v2.4 CMYK 配置，保证开箱演示可用。
- **环境变量**：
  - `SPD_DATA_DIR`（默认 `./data`）：数据根目录；
  - `SPD_MAX_DIMENSION`（默认 `3000`）：允许的图像最长边像素；
  - `SPD_MAX_UPLOAD_MB`（默认 `50`）：上传大小上限。

## 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由
│   ├── release_api.py   # 发布/门禁 HTTP API
│   ├── releases.py      # 配置族/草稿/基线/门禁/发布/撤回/恢复/审计 服务
│   ├── pipeline.py      # 色彩流水线、导出（支持按快照字节重跑）
│   ├── retouch.py       # 局部 CMYK 修色：统一整数偏移搜索、残余色差、TIFF 成品
│   ├── colorscience.py  # CIEDE2000、热图（高精度 Lab 由 lcms_native 取得）
│   ├── profiles.py      # ICC 配置管理与严格校验
│   ├── regions.py       # 框选区域统计
│   ├── database.py      # SQLite（含发布模块表、不可变审计触发器、写事务锁）
│   ├── lcms_native.py   # liblcms2 ctypes 绑定（TYPE_Lab_DBL 高精度转换）、内置配置生成
│   ├── config.py
│   └── static/          # 前端（软打样 + 局部修色 + 发布门禁三个视图）
├── tests/               # pytest 端到端测试（软打样 + 局部修色 + 发布门禁）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

### 运行测试

```bash
pip install -r requirements.txt pytest httpx
pytest -q
```

测试覆盖：软打样原有全链路与校验，以及局部 CMYK 修色（参数边界校验、统一偏移搜索与 ΔE/绝对值和/字典序选优、通道与总墨量逐像素约束、零偏移 ΔE≈0、不可达不出 TIFF、选区外 TIFF 字节不变且嵌入原印刷 ICC、ICC 缺失/摘要不符拒绝、修色 ID 确定性、`(1,23,4,5)` 与 `(12,3,4,5)` 等无分隔拼接歧义及目标 Lab/容差 1e-7 级差异的身份区分、相同请求字节级复用、缓存报告/预览被删改或被他矩形报告顶替时拒绝复用并重算、下载端点摘要校验、软打样 PNG 嵌入显示 ICC），以及发布门禁模块（配置族/草稿 CMYK 校验、基线固化与参数校验、相同快照通过、异配置检出变化像素并拒绝发布、门禁 run 确定性与历史复现、发布后配置可选与不可变文件名、期望版本号 409、撤回/恢复状态机、审计哈希链与 UPDATE/DELETE 触发器拦截）。
