import type {
  CatalogModel,
  ProbeMatrixJob,
  ProbeMatrixResult,
  ProbeRealm,
} from './types';

export interface ProbeModelRow {
  /** 去 realm 前缀后的模型名；矩阵输入与结果都用它做跨版本合并键 */
  name: string;
  displayName: string;
  realms: ProbeRealm[];
}

export interface ProbeSelectionStats {
  requests: number;
  accountWaves: number;
  estimatedSeconds: number;
}

export type ProbePollDecision =
  | 'continue'
  | 'terminal'
  | 'lost'
  | 'denied'
  | 'retry';
export type ProbeSelectionLimit = 'accounts' | 'models' | 'requests' | null;

/** 上游协议前缀只属于路由，不是模型显示名。 */
export function normalizeProbeModelName(value: string): string {
  const text = String(value || '').trim();
  if (/^cn:/i.test(text)) return text.slice(3).trim();
  if (/^global:/i.test(text)) return text.slice(7).trim();
  return text;
}

/**
 * 合并国内版/国际版模型目录。
 *
 * 同名模型只占一行，但 realms 保留两边来源；探测结果仍带 realm，因此界面
 * 永远不会把“国内版可用”误当成“国际版也可用”。
 */
export function mergeProbeModels(
  cnModels: CatalogModel[],
  globalModels: CatalogModel[],
): ProbeModelRow[] {
  const rows = new Map<string, ProbeModelRow>();
  const add = (items: CatalogModel[], realm: ProbeRealm) => {
    for (const item of items || []) {
      const name = normalizeProbeModelName(item.id);
      if (!name) continue;
      const current = rows.get(name);
      if (current) {
        if (!current.realms.includes(realm)) current.realms.push(realm);
        if (!current.displayName && item.name) current.displayName = item.name;
      } else {
        rows.set(name, {
          name,
          displayName: item.name || name,
          realms: [realm],
        });
      }
    }
  };
  add(cnModels, 'cn');
  add(globalModels, 'global');
  return [...rows.values()];
}

/** 仅计算最低等待：上游真实响应时间通常会让它更长，界面文案会明确“最少”。 */
export function probeSelectionStats(
  accountCount: number,
  modelCount: number,
  accountConcurrency: number,
  intervalMs = 500,
): ProbeSelectionStats {
  const accounts = Math.max(0, Math.floor(accountCount || 0));
  const models = Math.max(0, Math.floor(modelCount || 0));
  const concurrency = Math.max(1, Math.floor(accountConcurrency || 1));
  const interval = Math.max(500, Math.floor(intervalMs || 500));
  const accountWaves = accounts ? Math.ceil(accounts / concurrency) : 0;
  return {
    requests: accounts * models,
    accountWaves,
    estimatedSeconds: Math.ceil(accountWaves * models * interval / 1000),
  };
}

/** 发送按钮的唯一判据；组件不得绕开它自行判断。 */
export function canStartProbe(
  busy: boolean,
  active: boolean,
  accountCount: number,
  modelCount: number,
): boolean {
  return !busy && !active &&
    accountCount > 0 && accountCount <= 100 &&
    modelCount > 0 && modelCount <= 50 &&
    accountCount * modelCount <= 1000;
}

/** Why the current selection cannot start, so the UI never leaves a mute disabled button. */
export function probeSelectionLimit(
  accountCount: number,
  modelCount: number,
): ProbeSelectionLimit {
  if (accountCount > 100) return 'accounts';
  if (modelCount > 50) return 'models';
  if (accountCount * modelCount > 1000) return 'requests';
  return null;
}

export function isProbeJobActive(job: Pick<ProbeMatrixJob, 'state'> | null): boolean {
  return !!job && (
    job.state === 'queued' ||
    job.state === 'running' ||
    job.state === 'cancelling'
  );
}

/**
 * 轮询状态机：任务快照决定继续/终态；HTTP 404 表示任务已被新任务替换或
 * 服务重启丢失，必须释放本地 job 引用后允许关闭与重新开始。
 */
export function probePollDecision(
  job: Pick<ProbeMatrixJob, 'state'> | null,
  error?: unknown,
): ProbePollDecision {
  const status = (error as {response?: {status?: number}} | undefined)
    ?.response?.status;
  if (status === 404) return 'lost';
  if (status === 401 || status === 403) return 'denied';
  if (error !== undefined) return 'retry';
  if (!job) return 'terminal';
  return isProbeJobActive(job) ? 'continue' : 'terminal';
}

/** 结果键包含 realm，同名模型在两个版本的响应不会互相覆盖。 */
export function probeCellResult(
  results: ProbeMatrixResult[],
  file: string,
  realm: ProbeRealm,
  model: string,
): ProbeMatrixResult | undefined {
  const name = normalizeProbeModelName(model);
  return results.find((item) =>
    item.account_file === file &&
    item.realm === realm &&
    normalizeProbeModelName(item.model) === name);
}
