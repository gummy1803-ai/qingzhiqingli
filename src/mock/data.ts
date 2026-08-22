import dayjs from 'dayjs';
import customParseFormat from 'dayjs/plugin/customParseFormat';
import type { SignalName, DataPoint, VehicleData } from '../pages/Dashboard/types';
import logger from '../utils/logger';

dayjs.extend(customParseFormat);

/** 时间戳格式：年/月/日 时:分:秒（月日不补零，如 2026/8/7 20:52:10） */
const TS_FMT = 'YYYY/M/D HH:mm:ss';

/** 全部车辆 ID */
const VEHICLE_IDS = ['212', '345'] as const;

/** 数据点数量：最近 1 小时，每秒 1 条 */
const N = 3600;

/** 日志 tag */
const TAG = 'mock';

/** 确定性伪随机（xorshift32），同种子生成相同数据，便于调试与快照 */
function makeRng(seed: number): () => number {
  let s = seed >>> 0 || 1;
  return () => {
    s ^= s << 13; s >>>= 0;
    s ^= s >>> 17;
    s ^= s << 5; s >>>= 0;
    return s / 0xffffffff;
  };
}

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

/** 为一辆车生成最近 1 小时数据（3600 条/信号，每秒 1 条） */
function generateVehicleData(vehicleId: string, start: dayjs.Dayjs): VehicleData {
  const rng = makeRng(vehicleId === '212' ? 20260807 : 20260808);
  const warmup = 30; // 启动阶段（秒）
  const runTimeBase = vehicleId === '212' ? 5230 : 8710; // 累计运行时长基准(h)

  logger.info(TAG, `车辆 ${vehicleId} 开始生成数据`, {
    start: start.format(TS_FMT),
    pointsPerSignal: N,
    warmupSeconds: warmup,
  });

  const signals: Record<SignalName, DataPoint[]> = {
    FC_CurrOut: [],
    FC_VoltOut: [],
    FC_NetPwrOut: [],
    FC_MinCellVoltage: [],
    FC_MinVoltageChannel: [],
    FC_AvgCellVoltage: [],
    FC_AvgCellVoltDev: [],
    FC_VehicleIsolationR: [],
    FC_RunTime_Hours: [],
  };

  for (let i = 0; i < N; i++) {
    const ts = start.add(i, 'second').format(TS_FMT);

    // 阶段切换日志(只在边界打一次,避免刷屏)
    if (i === 0) {
      logger.info(TAG, `车辆 ${vehicleId} 进入启动阶段(电流低)`, { i });
    } else if (i === warmup) {
      logger.info(TAG, `车辆 ${vehicleId} 启动完成,进入运行阶段(电流波动)`, { i });
    }

    // FC_CurrOut: 50-400A，启动阶段低(50→100)，运行中 200 上下波动
    const cur =
      i < warmup
        ? clamp(50 + (i / warmup) * 50 + (rng() - 0.5) * 10, 50, 400)
        : clamp(200 + Math.sin(i / 50) * 80 + (rng() - 0.5) * 30, 50, 400);

    // FC_VoltOut: 200-400V，与电流负相关
    const volt = clamp(
      300 + Math.sin(i / 80) * 40 + (rng() - 0.5) * 20 + (300 - cur) / 6,
      200,
      400,
    );

    // FC_NetPwrOut: 0-150kW，= 电流×电压/1000 加噪声
    const pwr = clamp((cur * volt) / 1000 + (rng() - 0.5) * 3, 0, 150);

    // FC_MinCellVoltage: 2.5-4.5V，偶发骤降到 2.0V（约1%概率，模拟故障）
    let minV = 3.6 + Math.sin(i / 30) * 0.3 + (rng() - 0.5) * 0.15;
    if (rng() < 0.01) {
      minV = 2.0 + rng() * 0.1;
      logger.info(TAG, `车辆 ${vehicleId} 单体电压骤降(故障模拟)`, {
        i, ts, minV: +minV.toFixed(3),
      });
    }
    minV = clamp(minV, 2.0, 4.5);

    // FC_AvgCellVoltage: 3.0-4.2V
    const avgV = clamp(3.6 + Math.sin(i / 40) * 0.2 + (rng() - 0.5) * 0.1, 3.0, 4.2);

    // FC_AvgCellVoltDev: -0.3 ~ +0.3V（随机波动）
    const dev = clamp((rng() - 0.5) * 0.6, -0.3, 0.3);

    // FC_VehicleIsolationR: 500-2000kΩ，偶发掉到 300（约0.5%警告）
    let iso = 500 + rng() * 1500;
    if (rng() < 0.005) {
      iso = 300 + rng() * 50;
      logger.info(TAG, `车辆 ${vehicleId} 绝缘电阻异常下降(警告)`, {
        i, ts, iso: +iso.toFixed(1),
      });
    }

    // FC_MinVoltageChannel: 1-120 整数（模拟单体编号）
    const ch = 1 + Math.floor(rng() * 120);

    // FC_RunTime_Hours: 累计递增（0-10000h）
    const rt = runTimeBase + i / 3600;

    signals.FC_CurrOut.push({ timestamp: ts, value: +cur.toFixed(2) });
    signals.FC_VoltOut.push({ timestamp: ts, value: +volt.toFixed(2) });
    signals.FC_NetPwrOut.push({ timestamp: ts, value: +pwr.toFixed(2) });
    signals.FC_MinCellVoltage.push({ timestamp: ts, value: +minV.toFixed(3) });
    signals.FC_MinVoltageChannel.push({ timestamp: ts, value: ch });
    signals.FC_AvgCellVoltage.push({ timestamp: ts, value: +avgV.toFixed(3) });
    signals.FC_AvgCellVoltDev.push({ timestamp: ts, value: +dev.toFixed(3) });
    signals.FC_VehicleIsolationR.push({ timestamp: ts, value: +iso.toFixed(1) });
    signals.FC_RunTime_Hours.push({ timestamp: ts, value: +rt.toFixed(4) });
  }

  logger.info(TAG, `车辆 ${vehicleId} 数据生成完成`, {
    signals: Object.keys(signals).length,
    pointsPerSignal: N,
  });
  return { vehicleId, signals };
}

// ---------- 模块加载时生成“最近 1 小时”数据 ----------

const END = dayjs();
const START = END.subtract(1, 'hour');

// 两车共享同一时间轴（都从 START 起，每秒 1 条），预计算 ms 用于快速切片
const TIME_AXIS_MS: number[] = (() => {
  const arr = new Array<number>(N);
  for (let i = 0; i < N; i++) arr[i] = START.add(i, 'second').valueOf();
  return arr;
})();

const mockDB: Record<string, VehicleData> = {
  '212': generateVehicleData('212', START),
  '345': generateVehicleData('345', START),
};

logger.info(TAG, '全部 mock 数据生成完成', {
  vehicles: [...VEHICLE_IDS],
  range: { start: START.format(TS_FMT), end: END.format(TS_FMT) },
  pointsPerSignal: N,
});

// ---------- 对外 API ----------

/**
 * 根据时间范围切片返回某辆车的数据。
 * @param vehicleId 车辆ID（'212' 或 '345'）
 * @param start     起始时间（格式 YYYY/M/D HH:mm:ss，如 2026/8/7 20:52:10）
 * @param end       结束时间（同上）
 * @returns 切片后的车辆数据；若 vehicleId 不存在或时间格式非法返回 null
 */
export function getMockData(
  vehicleId: string,
  start: string,
  end: string,
): VehicleData | null {
  const v = mockDB[vehicleId];
  if (!v) {
    logger.warn(TAG, 'getMockData: 车辆不存在', {
      vehicleId,
      available: Object.keys(mockDB),
    });
    return null;
  }

  const sMs = dayjs(start, TS_FMT).valueOf();
  const eMs = dayjs(end, TS_FMT).valueOf();
  if (Number.isNaN(sMs) || Number.isNaN(eMs)) {
    logger.warn(TAG, 'getMockData: 时间格式解析失败', {
      start, end, sMs, eMs, expectedFormat: TS_FMT,
    });
    return null;
  }

  // 先按共享时间轴选出命中索引，再对所有信号统一取值（避免重复字符串解析）
  const idxs: number[] = [];
  for (let i = 0; i < TIME_AXIS_MS.length; i++) {
    const t = TIME_AXIS_MS[i];
    if (t >= sMs && t <= eMs) idxs.push(i);
  }

  const result: VehicleData = {
    vehicleId,
    signals: {} as Record<SignalName, DataPoint[]>,
  };
  (Object.keys(v.signals) as SignalName[]).forEach((sig) => {
    const arr = v.signals[sig];
    result.signals[sig] = idxs.map((i) => arr[i]);
  });

  logger.info(TAG, 'getMockData 切片完成', {
    vehicleId, start, end,
    points: idxs.length,
    signals: Object.keys(v.signals).length,
  });
  return result;
}

/** 全部车辆数据（便于直接渲染，不切片） */
export const mockVehicles: VehicleData[] = VEHICLE_IDS.map((id) => mockDB[id]);

/** 信号列表（便于 UI 渲染选择器） */
export const SIGNAL_LIST: SignalName[] = [
  'FC_CurrOut',
  'FC_VoltOut',
  'FC_NetPwrOut',
  'FC_MinCellVoltage',
  'FC_MinVoltageChannel',
  'FC_AvgCellVoltage',
  'FC_AvgCellVoltDev',
  'FC_VehicleIsolationR',
  'FC_RunTime_Hours',
];

/** 全部车辆ID */
export const VEHICLE_ID_LIST: string[] = [...VEHICLE_IDS];

/** 数据时间范围（字符串，便于 UI 默认填充） */
export const MOCK_TIME_RANGE: { start: string; end: string } = {
  start: START.format(TS_FMT),
  end: END.format(TS_FMT),
};

export default mockVehicles;
