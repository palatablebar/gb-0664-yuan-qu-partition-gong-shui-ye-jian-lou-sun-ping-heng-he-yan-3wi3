# 园区分区供水与夜间漏损核验

以计量边为唯一事实归属分区，跨区转供在两侧分别记入并在全园合计相消；使用误差上下界判断漏损。

使用者按采信、规则复算、异常复核和归档顺序处理一批数据。启动后自动装入异常样例，切换基准样例可比较两者结论。

## 启动与测试
```sh
docker compose up --build -d
docker compose exec -T app pytest -q
docker compose config --quiet
```

浏览器打开 [工作台](http://localhost:18203)。

## 业务模块

- 计量采信：读取台账并核对原始列与行号，入口 `/intake` 与 `GET /api/intake`。
- 单位契约：将升与立方米统一并裁决重复键，入口 `/contract` 与 `GET /api/contract`。
- 拓扑归属：核查计量目录、方向与环，入口 `/model` 与 `GET /api/model`。
- 水量平衡：计算各分区与夜间净水量，入口 `/calculate` 与 `GET /api/calculate`。
- 质量隔离：按原因码定位异常证据，入口 `/quarantine` 与 `GET /api/quarantine`。
- 阈值复核：复算误差上下界与水量守恒，入口 `/reconcile` 与 `GET /api/reconcile`。
- 批次归档：幂等重跑与版本复核，入口 `/batches` 与 `GET /api/batches`。
- 夜间漏损发布：17:00 后发布前晚结论，迟到补录选择重开当班或移交下一班，入口 `/night` 与 `GET /api/night`。

## 演示
选择样例后运行，核对业务结论与来源行。异常行保留原值和原因码。复核提交使用当前版本；旧版本返回 409。CSV证据只保留一个归档入口。

夜间漏损页面按夜次对照首版正式结论、当前修订与补录后净水量，逐区标出净水量变化和漏损判断是否改变。补录到达后必须显式选择：
- 重开当班：新增一条修订，补录并入本期净水量，原修订与原读数保留在证据中。
- 保留原结论并移交下一班：不新增修订、不改本期净水量，影响登记到下一班的承接区。

同一补录（逻辑读数相同）重试不会新增修订或重复计入。发布写入失败整体回滚，旧版正式结论继续有效。每条记录可通过页面的「追回原读数」查到历次取值。

## API
- `POST /api/runs`：JSON 中可指定 `sample`、`text` 与 `params`。
- `POST /api/runs/{run_id}/review`：提交 `revision`、`decision`。
- `GET /api/runs/{run_id}/evidence.csv`：来源证据。
- `GET /api/night?night=YYYY-MM-DD`：夜次详情，含正式结论、补录前后净水量与采用依据。
- `POST /api/night/{night}/publish`：发布前晚结论。
- `POST /api/night/{night}/backfill`：登记迟到补录，JSON 含 `text`。
- `POST /api/night/{night}/backfill/{key}/reopen`：重开当班。
- `POST /api/night/{night}/backfill/{key}/carry`：保留原结论并移交下一班。
- `GET /api/night/{night}/readings/{record_id}`：追回某记录历次取值。
- `GET /api/night/{night}/evidence.csv`：夜次证据，含被替换的原读数。
- `GET /docs`：接口定义。

## 数据保存
SQLite 位于命名卷 records。相同内容、参数与规则版本只产生一条运行记录。仅需重启时执行 `docker compose restart app`。要重新初始化样例数据，先备份所需记录，再执行 `docker compose down -v` 与 `docker compose up --build -d`；此操作会删除当前项目的历史与复核数据。

## 定义与边界
输入行数=有效行+隔离行+空行。供水量=售水量+授权用水+各区残差。value为原值，unit支持m3与L，value_m3为立方米。error_pct在0至10之间，误差按每条计量绝对值相加；不假设独立随机误差。时间必须含偏移，先转UTC；夜间为中国标准时间[00:00,05:00)。

## 已知不覆盖
不推断缺失表的读数，不做管网压力仿真。计量目录只允许无环转供，复杂回流管网需先另建可辨识模型。

## 目录
`app/modules/` 为七个业务模块，`sample_data/` 提供基准与异常台账，`tests/` 包含模块测试与端到端测试，`docs/` 保存定义与深度登记。
