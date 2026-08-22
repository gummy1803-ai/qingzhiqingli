/** 燃料电池信号名联合类型 */
export type SignalName =
  | 'FC_CurrOut'
  | 'FC_VoltOut'
  | 'FC_NetPwrOut'
  | 'FC_MinCellVoltage'
  | 'FC_MinVoltageChannel'
  | 'FC_AvgCellVoltage'
  | 'FC_AvgCellVoltDev'
  | 'FC_VehicleIsolationR'
  | 'FC_RunTime_Hours';

/** 单个数据点 */
export interface DataPoint {
  timestamp: string; // 格式 "2026-08-22 20:52:10"
  value: number;
}

/** 单辆车的全部信号数据 */
export interface VehicleData {
  vehicleId: string;
  signals: Record<SignalName, DataPoint[]>;
}

/** 看板筛选状态 */
export interface FilterState {
  vehicleId: string;
  startTime: string;
  endTime: string;
  selectedSignals: SignalName[];
}
