/** 轻量日志模块(零依赖,底层 console),便于排查报错 */

type LogLevel = 'info' | 'warn' | 'error';

const LEVEL_TAG: Record<LogLevel, string> = {
  info: 'INFO',
  warn: 'WARN',
  error: 'ERROR',
};

/** 时间戳:年/月/日 时:分:秒.毫秒(与业务数据时间格式保持一致风格) */
function timestamp(): string {
  const d = new Date();
  const p = (n: number, l = 2) => String(n).padStart(l, '0');
  return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

function emit(level: LogLevel, tag: string, msg: string, extra?: unknown): void {
  const line = `[${timestamp()}] [${LEVEL_TAG[level]}] [${tag}] ${msg}`;
  const fn =
    level === 'error' ? console.error : level === 'warn' ? console.warn : console.log;
  if (extra !== undefined) fn(line, extra);
  else fn(line);
}

export const logger = {
  info: (tag: string, msg: string, extra?: unknown) => emit('info', tag, msg, extra),
  warn: (tag: string, msg: string, extra?: unknown) => emit('warn', tag, msg, extra),
  error: (tag: string, msg: string, extra?: unknown) => emit('error', tag, msg, extra),
};

export default logger;
