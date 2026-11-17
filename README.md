# 滨海曲面场加工归档

本地 Python 3.11 标准库工具，供滨海电磁实验室的数据员登记求解器曲面数据，供质控员执行精确采样、逐面环流、边界和面总量核账。SQLite 保存历史源修订、固定计划及完整成果。无需安装或网络。

## 一天的工作

1. 打开归档：缺失结构初始化，已有数据和当前引用保留；样例不会自动装载。
2. 接收求解器节点及三角面：检查真实三维非退化性，保留节点和面身份。
3. 核对普通边方向：节点 ID 字符串序决定规范方向，面保留求解器绕序。
4. 导入完整节点标量、边积分、面总量：三类身份必须精确覆盖，登记整批修订。
5. 质控员指定历史修订及有序重心采样队列，冻结分析计划。
6. 执行精确插值和逐面有向环流，生成边界、内部残余和有符号差额。
7. 成果与完成回执共同发布；失败可重试，成功重放返回原成果。
8. 上游更正后按旧成果 ID 读取、重开及导出，离库核账继续使用原输入。

## 数据口径

输入 mesh={'nodes': {ID: (Fraction, Fraction, Fraction)}, 'faces': {ID: (v0,v1,v2)}}，一批 1–60 个面。ID 是非空字符串。每面三个不同节点且三维叉积非零；每条普通边至多有两个关联面。同坐标不同身份不会焊接，重复几何合法。坐标无需位于 XY 平面。

fields 含 node_scalars、edge_integrals、face_totals 三个字典；边键必须为 (较小节点ID,较大节点ID)。所有坐标、字段和权重必须为 Fraction，不接受 int、bool、float 或 None 替代。零、负数和大分子分母合法。节点、边、面字段各恰好覆盖完整身份集合。输入及返回容器均独立。

求解器采用统一长度单位；节点场单位一致，边积分和面总量采用同一核账单位。程序不自动换算单位。采样位置和值按原面角顺序作重心线性组合；不排序权重，不跨面搜索。样本为 {'face': ID, 'weights': (w0,w1,w2)}，三个非负 Fraction 严格和为 1，保留顺序和重复项；空队列仍计算全部账。

面环流为三条规范边积分乘各次面边符号后求和。边界边只关联一个面；内部边关联两个面。反向共边抵消，同向共边留下真实内部残余，程序不自动翻面。面总量按原值保存，差额为环流减面总量；非零差额是质控结论，可正常发布。V1 使用普通端点对边，不进行坐标吸附、曲面修复或自动定向。

## 公开 Python API

从仓库根目录运行，无需包安装：

```python
from fractions import Fraction as F
from surface.sources.storage import ArchiveStore
from surface.sources.catalog import SourceCatalog
from surface.tasks.plans import PlanService
from surface.tasks.execution import AnalysisService
from surface.results.reading import ResultReader
from surface.results.exporting import ResultExporter
from surface.results.checking import load_solver_sample, check_export

mesh, fields, samples = load_solver_sample("samples/normal.json")
store = ArchiveStore.open("quality.sqlite")
try:
    sources = SourceCatalog(store)
    receipt = sources.put_source("solver-1", mesh, fields)
    revision_id = receipt["revision_id"]
    plan_id = PlanService(store).create_plan("qc-1", revision_id, samples)
    result_id = AnalysisService(store).execute(plan_id)
    result = ResultReader(store).read_result(result_id)
    data = ResultExporter(ResultReader(store)).export_result(result_id)
    print(result["payload"]["circulations"])
    print(check_export(data))
finally:
    store.close()
```

显式缝合派生修订只用新入口，参数与返回外壳和 put_source 一致：

```python
seams = [(("A", "B"), ("X", "Y")), (("B", "C"), ("Y", "Z"))]
derived = sources.build_stitched(receipt["revision_id"], seams)
print(derived)                                  # {'revision_id': ..., 'fingerprint': ...}
print(sources.read_source(derived["revision_id"])["stitch"]["face_map"])
plan_id = PlanService(store).create_plan("qc-stitched", derived["revision_id"], samples)
result_id = AnalysisService(store).execute(plan_id)
print(ResultReader(store).read_result(result_id)["payload"]["kind"])  # stitched-analysis-v1
```

put_source 返回 {'revision_id': int, 'fingerprint': str}；read_source(revision_id) 返回 revision_id、fingerprint、mesh、fields 的独立快照（派生修订另有 stitch，见下节）；current_revision() 返回当前 ID 或 None。每个新源请求产生完整新修订。相同源请求及完整规范内容返回原回执，即使已有后续更正也不倒退当前引用。源比较包含坐标、面绕序和全部字段，不只比较摘要。

create_plan 返回整数 ID；PlanService.read_plan 返回 AnalysisPlan。计划冻结确切修订、摘要、完整队列和口径：普通修订为 ordinary-surface-v1，派生修订为 stitched-surface-v1。源请求与计划请求使用两个独立空间。同号异绑定抛 surface.sources.revisions.ConflictError；非法领域值抛 ValueError，缺少修订、计划或成果抛 KeyError。数据库异常原样传播。

execute 返回整数成果 ID。read_result 返回 {'result_id', 'fingerprint', 'payload'}；payload 含 kind、binding、完整 input、requests、samples、circulations、face_totals、balance。kind 为 ordinary-analysis-v1 或 stitched-analysis-v1，与绑定口径一致。balance 明确保存逐面差额、边界及内部各边项和总账。完成计划重放返回原成果，不重算；损坏成果明确报错。读取和导出只访问成果表，不访问当前源或执行器。

ArchiveStore 管理单连接及事务；SourceCatalog、PlanService、AnalysisService 和 ResultReader 共同使用该连接。各写入由所属服务提交，禁止嵌套事务。不提供多线程调度或进程恢复。store.fault 默认为 None；测试可安装接收阶段字符串的函数并抛异常。阶段包括 source_after_revision/source_after_request/source_after_head、plan_after_insert、compute_after_samples、publication_after_result/publication_after_receipt、export_before_encode。任何异常传播给调用者，失败不得伪装成功。

## 精确成果与离库核账

导出是确定性 ASCII JSON 字节，外壳为 format=surface-exact、version=1、payload。$fraction 保存既约整数分子及正分母，$tuple 保留元组，$map 保存稳定排序的键值项，列表保持顺序。拒绝重复 JSON 成员、重复映射身份、非既约分数、非法分母及不支持版本。成果携带完整源及计划摘要。

ResultRepository 验证载荷结构、完整身份和固定绑定；读取不会补算或修复数值。check_export(bytes) 是显式独立复核：仅遍历导出原始面绕序、坐标和固定字段，重新核对样本、环流及全部账；不调用任务执行器或场计算函数，不连接源库。V2 导出同样只消费已保存的源输入、配对和映射，独立核对商身份、邻域、方向、字段、源角采样与环流总账，不调用生产缝合构建器。可用 python3.11 -B -m surface.results.checking --check 导出文件 来核验自行保存的导出。

## 显式缝合派生修订（V2）

STITCHING.md 定义完整新增能力。SourceCatalog.build_stitched(source_revision, seams) 只接受本库已有普通 V1 修订，缺失抛 KeyError，把派生修订当参数抛 ValueError；每次成功都新增一个完整派生修订并推进 current_revision，空配对也如此。它不引入请求号或重放，因此同一源加同一配对提交两次会得到两个不同 revision_id。派生修订可原样传给 PlanService.create_plan，计划口径固定为 stitched-surface-v1，成果 payload.kind 为 stitched-analysis-v1；旧计划与 ordinary-analysis-v1 路径完全不变。

read_source 对派生修订返回 revision_id、fingerprint、mesh、fields、stitch。mesh 在 nodes、faces、edges、face_edges 之外增加 edge_endpoints；edges 为升序 edge_id 列表，edge_id 是合并组内原规范端点对的字典序最小者，不是商端点对。fields 的三张字典分别以商节点、独立 edge_id、原面 ID 为键。stitch 保存 source_revision、source_digest、source_input、按提交顺序转元组的 seams、node_map、edge_map、face_map。派生摘要为 canonical_digest({'mesh': mesh, 'fields': fields, 'stitch': stitch})，且 stitch.source_digest 等于 canonical_digest(stitch.source_input)。

FieldBundle.edge_value(start, end, *, edge_id=None) 在 V2 以商顶点 ID 取有向积分：省略 edge_id 时唯一候选返回该值、无边抛 KeyError、多条平行候选抛 ValueError；给出 edge_id 时按指定独立边取值、反向取负；边或端点不存在抛 KeyError，端点不属于该边或 start==end 抛 ValueError。普通 V1 的两参数调用保持原样，传入 edge_id 会被拒绝。

不变量：商顶点以所属原节点 ID 字符串序最小者命名，坐标取原精确值，原面 ID 不变；每条边唯一归属一个商边，未配对平行边保留各自 edge_id 与积分；配对端点坐标逐一精确相等，节点标量与换向后的边积分必须完全相等，不作平均，冲突即拒绝；面总量保持原值，翻面不取反。各连通分量以源面 ID 最小者保持原绕序，其余面由内边两侧符号相反唯一确定（翻面时 faces 取 (v0,v2,v1)，face_map.corners 相应为 (0,2,1)）；同分量方向矛盾、顶点邻域非圆盘/半圆盘、孤立点、边度超二均拒绝。采样始终引用原面 ID，权重对应源角顺序，翻面只改定向，不移动采样点、不重排权重也不去重。

读取、计划解析、成果读取与导出只解析已保存关联，不重跑并查集、不重新定向、不转移字段，也不调用 build_stitched 补算；旧 V1 库直接打开，不回填或改写旧成果。人工核账的独立小例为 parallel：配对 AB↔XY、BC↔YZ 后商结构 3 顶点 4 边 2 面，AC 与 XZ 未配对且同端点，积分分别是 2 和 5；f1 环流 1，f2 翻面后环流 2，边界 3，内部 0，面总量 0 与 -3，差总额 6；f2 源权重 (1/2,1/3,1/6) 的样本值 4/3、位置 (1/3,1/6,0)；无配对时各普通边照常共享，圆柱六面缝合得 6 顶点 12 边，Möbius 端缝反转与 A 处分离面扇均须拒绝。

## 手算样例与运行
samples/normal.json 为统一单位下两面 f1=(A,B,C)、f2=(B,D,C)。AB=1/3，AC=1/7，BC=2/5，BD=3/7，CD=-1/2，节点标量为 0、2、4、6，面总量为 0、-2/3。f1 环流为 1/3+2/5-1/7=62/105；f2 为 3/7+1/2-2/5=37/70。共边抵消，边界为 47/42，总差为 25/14。f1 权重 (1/2,1/3,1/6) 的标量为 4/3。samples/invalid.json 保留相同字段但将 A、B、C 放到同一直线，必须拒绝。

```sh
python3.11 -B -m unittest discover -s tests -t . -v
python3.11 -B stitched_checks.py
python3.11 -B -m surface.results.checking --demo samples/normal.json
python3.11 -B -m surface.results.checking --stitched-demo
python3.11 -B stitched_checks.py --fixtures
```

运行清单为 .pointwise-runtime.json，setup 为空。demo 在测试临时目录建立归档，执行、发布、更正、关闭重开、导出及离库核账；断言后输出实际 POINTWISE_SCENARIO JSON。--demo 依次运行普通链与缝合链，--stitched-demo 单独运行缝合链：保存派生修订、分析平行边与翻面采样、更正上游、关闭重开后导出复核。stitched_checks.py 贯穿构建、原计划执行及离库核账，--fixtures 只验证全部原始夹具可由 V1 接收。临时目录退出即清理。完整测试失败返回非零。测试覆盖竖直面、重复几何身份、严格字段覆盖、请求冲突、非法权重、固定修订、重复采样、同向内部残余、源和成果事务回滚、重试、历史读取、精确大分数、损坏载荷及导出阶段失败；V2 另覆盖无配对成功、非法/重复/非边界配对、拓扑拒绝、字段冲突、非对称及重复采样、不同连通分量锚定、旧源与派生源共存、容器隔离、发布失败重试、旧库与派生修订磁盘重开及离库核账。SQLite 授权器实际拒绝源和计划读取时，成果读取导出仍须成功；实际触发器拒绝回执写入时，成果提交必须回滚。

## 代码职责

sources 负责输入、修订、请求绑定和当前引用；mesh 负责三维几何、普通关联、边界链、重心定位及一次性商曲面构建（stitching.py，仅 build_stitched 调用）；mesh/stitched.py 只解析和交叉核对已保存的商网格与来源映射；fields 负责完整字段绑定、插值、环流及核账；tasks 负责固定计划、确切输入解析、内存计算和统一发布；results 负责精确编码、固定载荷读取、导出和独立复核。26 个生产模块均接入上述公开链路。测试夹具围绕同一求解器批次，不在初始化时修改用户数据。
