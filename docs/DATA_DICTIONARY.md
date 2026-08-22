# 设备测试数据分析与自动报告助手 · 数据说明书

> 本文档面向"完全不懂代码、第一次接触燃料电池测试数据"的客户与运维人员。
> 文档用途:
> 1. 帮助客户理解每项指标的含义、单位、计算方式
> 2. 作为 AI 助手的知识库,客户向 AI 提问时 AI 据本文档回答
> 3. 让运维人员快速定位异常,知道每个数字背后的代码逻辑
>
> AI 助手必须熟读本说明书的全部内容,客户问任何关于数据含义、计算方式、异常判断的问题时,
> 都基于本文档的描述回答,不得编造。

---

## 一、数据来源与文件结构

### 1.1 项目目录结构

```
T02_设备测试数据分析与自动报告助手/
├── 企业资料包02_氢质氢离/   # 业务方提供的原始数据
│   ├── 01_耐久原始数据处理/  # 8 份 docx 耐久报告
│   └── 02_整车数据处理/     # 按车辆编号分目录的 CSV 分片
│       ├── 212/             # 车辆 212,89 个 CSV 分片
│       └── 345/             # 车辆 345,81 个 CSV 分片
├── src/                     # 核心代码模块
│   ├── data_loader.py       # 数据加载与清洗
│   ├── metrics.py           # 指标计算
│   ├── plots.py             # 可视化
│   ├── report.py            # 报告生成
│   ├── data_quality.py      # 数据质量扫描
│   └── email_alert.py       # 邮件报警
├── app.py                   # Streamlit 应用入口
├── run_e2e.py               # 端到端流程脚本
├── scan_hyd_zero.py         # 数据质量扫描脚本
└── reports/                 # 生成的报告与简报
```

### 1.2 CSV 文件名命名规则

格式: `<车辆编号>_<起时间>_<止时间>_CH0_<导入时间>.csv`

示例: `201480_202607071800_202607072359_CH0_20260807_225246.csv`

| 字段 | 含义 |
|------|------|
| `201480` | 车辆编号(目录名) |
| `202607071800` | 数据起始时间(yyyyMMddHHmm) |
| `202607072359` | 数据结束时间 |
| `CH0` | 通道号(固定) |
| `20260807_225246` | 数据导入时间戳 |

解析逻辑见 `src/data_loader.py::parse_csv_filename()`,不符合此规则的文件名会被跳过。

### 1.3 耐久 docx 文件命名规则

格式: `耐久<起>-<止>-<导入时间>.docx`

示例: `耐久0-5-20260606024937.docx` 表示 0~5 阶段的耐久数据。

每份 docx 含若干表格,每个表格为一个阶段的耐久测试数据,表格结构: 每行一个时刻、每列一个指标。

---

## 二、CSV 字段定义

> ⚠ **关键单位提示**: 经样本数据扫描,`FC_MinCellVoltage / FC_MaxCellVoltage / FC_AvgCellVoltage` 字段数值集中在 100~1000 区间(典型值约 600~900),**单位疑似 mV 而非 V**(燃料电池单片电压物理范围 0.6~0.9V,对应 600~900mV)。阅读相关统计值时请按 mV 理解。

### 2.1 时间与基础

| 字段名 | 含义 | 单位 | 备注 |
|--------|------|------|------|
| `Timestamp` | 采样时刻 | datetime | 必填列,空值会被剔除,作为时间轴基础 |
| `FC_RunTime_Hours` | 累计运行小时 | 小时 | 取末值 |
| `FC_RunTime_Min` | 累计运行分钟 | 分钟 | 取末值 |

### 2.2 整车运行指标

| 字段名 | 含义 | 单位 | 备注 |
|--------|------|------|------|
| `FC_VehicleKM` | 累计里程 | km | 取首末差值得到行驶里程;哨兵值 65535/-1/999/99 视为无效 |
| `FC_VehicleSpd` | 车速 | km/h | 物理范围 0~200,超出视为采集异常 |
| `FC_StartTimes` | 启动次数 | 次 | 取末值,负值视为无效 |
| `FC_ErrorCode` | 故障码 | 整数 | >0 视为有故障,=0 视为正常 |

### 2.3 氢耗指标

| 字段名 | 含义 | 单位 | 备注 |
|--------|------|------|------|
| `FC_HydCmPerHundred` | 百公里氢耗 | kg/100km | 物理范围 0~100,超出视为无效;若全 0 表示采集端故障 |
| `FC_HydCmInstts` | 瞬时氢耗 | kg/h | ≥0,负值视为无效 |

> **已知问题**: 车辆 345 的 `FC_HydCmPerHundred` 字段在所有 1,017,997 行中**全部为 0**,属于采集设备/上传流程问题,非过滤逻辑 bug。报告对应位置会显示 "-"。

### 2.4 单片电压一致性

| 字段名 | 含义 | 单位(疑似) | 物理范围 |
|--------|------|-----------|----------|
| `FC_MinCellVoltage` | 最小单片电压 | mV | 600~900 |
| `FC_MaxCellVoltage` | 最大单片电压 | mV | 600~900 |
| `FC_AvgCellVoltage` | 平均单片电压 | mV | 600~900 |
| `FC_MinVoltageChannel` | 最弱电压通道号 | 整数 | ≥0,出现频次最高即最弱 |

### 2.5 功率与效率

| 字段名 | 含义 | 单位 | 物理范围 |
|--------|------|------|----------|
| `FC_NetPwrOut` | 净输出功率 | kW | 0~100000 |
| `FC_CurrOut` | 输出电流 | A | 0~1000 |
| `FC_VoltOut` | 输出电压 | V | - |
| `TotalVoltage` | 总电压 | V | - |

### 2.6 氢系统状态

| 字段名 | 含义 | 单位 | 物理范围 |
|--------|------|------|----------|
| `FC_HSSHighPreu` | 高压氢瓶压力 | bar | ≥0 |
| `FC_HSSMidPre` | 中压氢瓶压力 | bar | ≥0 |
| `FC_HSSH2SOC` | 氢瓶 SOC | % | 0~100 |

---

## 三、指标计算逻辑

> 所有指标函数位于 `src/metrics.py`,输入均为 `load_vehicle_csvs()` 输出的 DataFrame,输出均为 dict。

### 3.1 整车运行概览 `vehicle_overview(df)`

| 输出指标 | 计算方式 | 依赖字段 |
|---------|---------|---------|
| 运行时长(h) | `(末时刻 - 首时刻).total_seconds() / 3600` | Timestamp |
| 采样点数 | `len(df)` | - |
| 起止时间 | `首时刻 → 末时刻` | Timestamp |
| 累计运行小时 | `FC_RunTime_Hours.iloc[-1]` | FC_RunTime_Hours |
| 累计运行分钟 | `FC_RunTime_Min.iloc[-1]` | FC_RunTime_Min |
| 里程末值(km) | 过滤后 `FC_VehicleKM` 末值 | FC_VehicleKM |
| 里程初值(km) | 过滤后 `FC_VehicleKM` 首值 | FC_VehicleKM |
| 行驶里程(km) | `里程末值 - 里程初值` | FC_VehicleKM |
| 平均车速(km/h) | 过滤后 `FC_VehicleSpd.mean()` | FC_VehicleSpd |
| 最高车速(km/h) | 过滤后 `FC_VehicleSpd.max()` | FC_VehicleSpd |
| 启动次数 | `FC_StartTimes.iloc[-1]` | FC_StartTimes |
| 百公里氢耗均值(kg) | 过滤后 `FC_HydCmPerHundred.mean()` | FC_HydCmPerHundred |
| 百公里氢耗峰值(kg) | 过滤后 `FC_HydCmPerHundred.max()` | FC_HydCmPerHundred |
| 瞬时氢耗均值(kg/h) | 过滤后 `FC_HydCmInstts.mean()` | FC_HydCmInstts |
| 故障码Top10 | `FC_ErrorCode.value_counts().head(10).to_dict()` | FC_ErrorCode |
| 故障总数 | `(FC_ErrorCode > 0).sum()` | FC_ErrorCode |
| 故障码种类 | `FC_ErrorCode.nunique()` | FC_ErrorCode |

**过滤逻辑**:
- `FC_VehicleKM`: 剔除哨兵值 `{65535, -1, 999, 99}` 和负值
- `FC_VehicleSpd`: 保留 `[0, 200)` 区间
- `FC_HydCmPerHundred`: 保留 `(0, 100)` 区间
- `FC_HydCmInstts`: 保留 `[0, ∞)` 区间
- `FC_ErrorCode`: 只保留 `> 0` 的记录
- `FC_StartTimes`: 只保留 `≥ 0` 的记录

### 3.2 单片电压一致性 `cell_voltage_consistency(df)`

| 输出指标 | 计算方式 | 依赖字段 |
|---------|---------|---------|
| FC_MinCellVoltage.{mean,min,max} | 过滤后统计量 | FC_MinCellVoltage |
| FC_MaxCellVoltage.{mean,min,max} | 过滤后统计量 | FC_MaxCellVoltage |
| FC_AvgCellVoltage.{mean,min,max} | 过滤后统计量 | FC_AvgCellVoltage |
| cell_diff.mean | 压差序列均值 | FC_MaxCellVoltage, FC_MinCellVoltage |
| cell_diff.max | 压差序列最大值 | FC_MaxCellVoltage, FC_MinCellVoltage |
| 最弱通道Top5 | `FC_MinVoltageChannel.value_counts().head(5)` | FC_MinVoltageChannel |

**过滤逻辑**:
- 单电压: 保留 `(0, 2000)` 区间(mV 疑似)
- 压差: 两端均在 `(0, 2000)` 内,且 `0 < diff < 50`(单片压差物理上限)
- 最弱通道: `FC_MinVoltageChannel >= 0`

**业务含义**:
- **压差 mean**: 反映单片一致性健康度,正常 10~30mV,> 50mV 提示有劣化
- **压差 max**: 反映瞬时异常,过大表示有采集错误或单片严重不均
- **最弱通道 Top1**: 出现频次最高的最弱通道号,需重点检修该通道

### 3.3 功率与效率 `power_summary(df)`

| 输出指标 | 计算方式 |
|---------|---------|
| FC_NetPwrOut.{mean,max} | 净功率统计量 |
| FC_CurrOut.{mean,max} | 电流统计量 |
| FC_VoltOut.{mean,max} | 输出电压统计量 |
| TotalVoltage.{mean,max} | 总电压统计量 |

**过滤逻辑**: 所有字段保留 `(0, 100000)` 区间。

### 3.4 氢系统状态 `h2_system(df)`

| 输出指标 | 计算方式 |
|---------|---------|
| FC_HSSHighPreu.{first,last,min,max} | 高压氢瓶压力起止与极值 |
| FC_HSSMidPre.{first,last,min,max} | 中压氢瓶压力起止与极值 |
| FC_HSSH2SOC.{first,last,min,max} | 氢瓶 SOC 起止与极值 |

**过滤逻辑**: 所有字段保留 `≥ 0` 区间。

**业务含义**:
- **first / last 对比**: 反映测试期间压力/SOC 衰减程度
- SOC 末值低表示氢瓶快空,需补充
- 高压末值远低于初值表示管路可能有泄漏

### 3.5 故障时间序列 `fault_time_series(df)`

返回所有故障发生时刻的子集 DataFrame,列含 `Timestamp, FC_ErrorCode, FC_SysFltRnk`(按可用列过滤)。

**过滤逻辑**: `FC_ErrorCode > 0`。

### 3.6 车速与氢耗时间序列 `vehicle_speed_profile(df)`

返回含 `Timestamp, FC_VehicleSpd, FC_HydCmInstts, FC_VehicleKM` 的时间序列,用于曲线绘制。

**过滤逻辑**: 各字段保留 `[0, 100000)` 区间。

---

## 四、异常值处理规则

### 4.1 哨兵值集合

源自 `src/data_loader.py::INVALID_SENTINELS = {65535, -1, 999, 99}`

这些值在工业数据采集系统中常用作"无效数据占位符",计算时一律剔除。

### 4.2 异常值打标 `mark_invalid(df)`

为以下列添加 `__<列名>_invalid` 布尔标记列,不影响原值:

```
FC_MinCellVoltage, FC_MaxCellVoltage, FC_AvgCellVoltage,
FC_CurrOut, FC_VoltOut, FC_NetPwrOut, FC_VehicleSpd,
FC_VehicleKM, FC_HydCmPerHundred, FC_HydCmInstts
```

判定规则: 值在哨兵集合内 **或** 数值化后 `< 0`(字符串列只比较哨兵值,不比较负值,避免 TypeError)。

### 4.3 各列物理范围过滤

| 字段类别 | 过滤区间 |
|---------|---------|
| 车速 | `[0, 200)` |
| 里程 | `[0, ∞)` 且非哨兵 |
| 单片电压 | `(0, 2000)` 疑似 mV |
| 单片压差 | `(0, 50)` 单位 mV |
| 输出功率/电流/电压 | `(0, 100000)` |
| 高压/中压压力/SOC | `[0, ∞)` |
| 百公里氢耗 | `(0, 100)` |
| 瞬时氢耗 | `[0, ∞)` |
| 故障码 | `> 0` |
| 启动次数 | `≥ 0` |

---

## 五、报告章节说明

报告由 `src/report.py::build_report_html()` 生成,共 7 章:

| 章节 | 内容 | 数据来源 |
|------|------|---------|
| 一、关键指标概览 | KPI 卡片网格 | `vehicle_overview` 主要指标 |
| 二、详细指标 | 表格列出所有计算结果 | `vehicle_overview / cell_voltage_consistency / power_summary / h2_system` |
| 三、单片电压一致性 | 曲线图 + 单位说明 | `fig_cell_voltage(df)` |
| 四、功率与电流 | 双 Y 轴曲线 | `fig_power_curve(df)` |
| 五、车速与瞬时氢耗 | 双 Y 轴曲线 | `fig_speed_hydrogen(df)` |
| 六、故障码分布 | 柱状图 Top10 | `fig_fault_bar(fault_top)` |
| 七、结论 | 文字建议 | 综合判断 |

报告体积控制: 通过 `_downsample()` 把原始数据降到 1001 个采样点绘制曲线,既保证趋势清晰又控制 HTML 体积(典型 250 KB)。

---

## 六、数据质量扫描

`src/data_quality.py::scan_df(df, vehicle, fields)` 对上传的 DataFrame 扫描以下 9 个关键字段:

```
FC_HydCmPerHundred, FC_HydCmInstts, FC_VehicleSpd,
FC_VehicleKM, FC_NetPwrOut, FC_CurrOut,
FC_MinCellVoltage, FC_MaxCellVoltage, FC_ErrorCode
```

对每个字段统计 0 值/哨兵值/NaN 占比,按以下规则判定风险等级:

| 风险等级 | 判定条件 |
|---------|---------|
| 高危 | 0 值占比 > 50% |
| 中危 | 0 < 0 值占比 ≤ 50% |
| 低危 | 0 值占比 = 0 |
| 无数据 | 总行数 = 0 |

整体风险 = 所有字段中最高风险等级;任何字段高危即整体高危。

发现高危时,在 Streamlit 页面用红色提示,并自动发邮件报警(需配置 `config/email_config.ini`)。

---

## 七、AI 助手回答规范

AI 助手必须严格基于本说明书回答客户问题,以下原则:

1. **不编造**: 没有出现在本文档的字段或计算方式,不要凭空生成
2. **给单位**: 提到任何数字都必须带上单位(尤其单片电压按 mV 理解)
3. **解释计算方式**: 客户问"百公里氢耗 6.92 是怎么算的"时,要回答"是过滤后 FC_HydCmPerHundred 字段的平均值,过滤规则是保留 (0, 100) 区间"
4. **解释异常**: 客户问"为什么 345 报告里百公里氢耗是 -"时,要回答"因为 345 车辆 CSV 中该字段全部为 0(原始数据源问题),被过滤后无有效数据"
5. **指引代码**: 客户想看代码时,可指出对应文件路径,如"压差计算逻辑见 src/metrics.py:129-146"
6. **不解读业务**: 涉及具体故障码诊断、车辆检修建议等业务判断,提示客户联系运维负责人

---

## 八、典型问答示例

### Q1: 报告里的"压差 mean=7.4"是什么意思?

**A**: 这是单片电压一致性的关键指标,反映电堆内部各单片电压的均衡度。计算方式: 对每个采样时刻,取 `FC_MaxCellVoltage - FC_MinCellVoltage` 得到瞬时压差,然后过滤掉两端的采集异常(要求两端都在 (0, 2000) mV 范围内,且瞬时压差 < 50mV),剩下的压差取平均值。单位疑似 mV,所以 7.4 应理解为 0.0074 V(7.4mV),属正常单片压差范围。代码见 `src/metrics.py:129-146`。

### Q2: 为什么车辆 345 的"百公里氢耗"显示是 "-"?

**A**: 因为 345 车辆的 CSV 数据中 `FC_HydCmPerHundred` 字段在全部 1,017,997 行中都是 0.0(原始数据源问题,不是过滤逻辑 bug)。该字段的过滤规则是"保留 (0, 100) 区间",0 值被剔除后无任何有效数据,所以报告显示 "-"。请采集方排查 345 车辆的采集设备/上传流程,可参考 `reports/345_raw_export.csv` 排查。

### Q3: "最弱通道 Top1: 27.0 (370903 次)"怎么理解?

**A**: 在每个采样时刻,电堆会记录电压最低的那片所在通道号(`FC_MinVoltageChannel`)。统计整个测试期间每个通道号作为"最弱通道"出现的频次,频次最高的就是"最弱通道 Top1"。212 车辆的 27 号通道作为最弱通道出现了 370,903 次,占采样总数 1,142,636 的约 32%,说明 27 号通道对应的单片持续是最低电压,需重点检修该通道。代码见 `src/metrics.py:148-158`。

### Q4: "行驶里程 6252 km"怎么算的?

**A**: 取 `FC_VehicleKM` 字段(累计里程),先过滤掉哨兵值 {65535, -1, 999, 99} 和负值,然后取末值减首值。212 车辆的末值是 27353 km,首值是 21101 km,差值 6252 km。注意: 这里取的是过滤后序列的首末,不是原始数据的首末,以避免异常值干扰。代码见 `src/metrics.py:48-57`。

### Q5: 报告里"故障码 Top10"的数字 127.0 是故障码吗?

**A**: 是的。计算方式: 把 `FC_ErrorCode` 字段过滤出 `> 0` 的记录(0 表示无故障),然后按故障码值统计出现次数,取次数最多的前 10 个。212 车辆故障码 127 出现了 6220 次,是出现频次最高的故障码,需运维查故障码表确定对应的具体故障类型。代码见 `src/metrics.py:91-103`。

---

## 九、术语速查表

| 术语 | 解释 |
|------|------|
| 电堆 | 燃料电池堆,由多片单片串联组成 |
| 单片 | 电堆内最小电压单元,正常电压 0.6~0.9V |
| 压差 | 同时刻最大单片电压 - 最小单片电压,反映一致性 |
| SOC | State of Charge,荷电状态,这里指氢瓶储氢百分比 |
| 哨兵值 | 65535/-1/999/99 等工业数据中的"无效占位符" |
| 降采样 | 为控制报告体积,把原始数据等间隔抽取为 1001 点绘制曲线 |
| KPI | Key Performance Indicator,关键指标 |
| Streamlit | Python Web 应用框架,本项目用它做交互界面 |
| Plotly | 交互式可视化库,本项目用它绘制曲线和柱图 |

---

> 文档版本: 1.0  |  生成时间: 2026-08-21  |  维护: T02 项目组
