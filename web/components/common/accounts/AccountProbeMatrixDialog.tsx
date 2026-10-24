'use client';

import {useEffect, useMemo, useState} from 'react';
import {Loader2, Play, Square, TriangleAlert, Zap} from 'lucide-react';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {accountApi, errText, modelApi} from '@/lib/api';
import {
  canStartProbe,
  isProbeJobActive,
  mergeProbeModels,
  probeCellResult,
  probePollDecision,
  probeSelectionLimit,
  probeSelectionStats,
  type ProbeModelRow,
} from '@/lib/account-probe';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import type {
  Account,
  ProbeCellStatus,
  ProbeMatrixJob,
  ProbeRealm,
} from '@/lib/types';

const STATUS_KEY: Record<ProbeCellStatus, string> = {
  pending: 'accounts.probeMatrixStatusPending',
  running: 'accounts.probeMatrixStatusRunning',
  cancelled: 'accounts.probeMatrixStatusCancelled',
  success: 'accounts.probeMatrixStatusSuccess',
  rate_limited: 'accounts.probeMatrixStatusRateLimited',
  quota_exhausted: 'accounts.probeMatrixStatusQuotaExhausted',
  unauthorized: 'accounts.probeMatrixStatusUnauthorized',
  model_unavailable: 'accounts.probeMatrixStatusModelUnavailable',
  upstream_error: 'accounts.probeMatrixStatusUpstreamError',
  budget_exhausted: 'accounts.probeMatrixStatusBudgetExhausted',
  request_error: 'accounts.probeMatrixStatusRequestError',
};

const STATUS_CLASS: Record<ProbeCellStatus, string> = {
  pending: 'text-muted-foreground',
  running: 'text-sky-600 dark:text-sky-400',
  cancelled: 'text-muted-foreground',
  success: 'text-emerald-600 dark:text-emerald-400',
  rate_limited: 'text-amber-600 dark:text-amber-400',
  quota_exhausted: 'text-orange-600 dark:text-orange-400',
  unauthorized: 'text-rose-600 dark:text-rose-400',
  model_unavailable: 'text-slate-600 dark:text-slate-300',
  upstream_error: 'text-rose-600 dark:text-rose-400',
  budget_exhausted: 'text-amber-600 dark:text-amber-400',
  request_error: 'text-rose-600 dark:text-rose-400',
};

const JOB_STATE_KEY: Record<ProbeMatrixJob['state'], string> = {
  queued: 'accounts.probeMatrixJobQueued',
  running: 'accounts.probeMatrixRunning',
  cancelling: 'accounts.probeMatrixJobCancelling',
  cancelled: 'accounts.probeMatrixCancelled',
  failed: 'accounts.probeMatrixFailed',
  finished: 'accounts.probeMatrixFinished',
};

const JOB_STORAGE_KEY = 'workbuddy-manager.probe-matrix.job';

function toggleValue<T>(items: T[], value: T): T[] {
  return items.includes(value)
    ? items.filter((item) => item !== value)
    : [...items, value];
}

export function AccountProbeMatrixDialog({
  accounts,
  open,
  onOpenChange,
  upstreamId,
}: {
  accounts: Account[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  upstreamId?: number | null;
}) {
  const t = useT();
  const [modelRows, setModelRows] = useState<ProbeModelRow[]>([]);
  const [selectedFiles, setSelectedFiles] = useState<string[]>([]);
  const [selectedModels, setSelectedModels] = useState<string[]>([]);
  const [accountConcurrency, setAccountConcurrency] = useState(2);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState('');
  const [startBusy, setStartBusy] = useState(false);
  const [cancelBusy, setCancelBusy] = useState(false);
  const [job, setJob] = useState<ProbeMatrixJob | null>(null);

  const running = isProbeJobActive(job);
  const currentJobId = job?.job_id ?? '';
  const stats = useMemo(
    () => probeSelectionStats(
      selectedFiles.length, selectedModels.length, accountConcurrency, 500),
    [selectedFiles.length, selectedModels.length, accountConcurrency],
  );
  const selectionLimit = probeSelectionLimit(
    selectedFiles.length,
    selectedModels.length,
  );

  useEffect(() => {
    if (!open) return;
    let disposed = false;
    setJob(null);
    setCatalogLoading(true);
    setCatalogError('');
    setSelectedFiles(accounts.map((item) => item.file));
    const restoredJobId = window.sessionStorage.getItem(JOB_STORAGE_KEY);
    if (restoredJobId) {
      accountApi.probeMatrix(restoredJobId)
        .then((restored) => {
          if (!disposed) {
            setJob(restored);
            if (!isProbeJobActive(restored)) {
              window.sessionStorage.removeItem(JOB_STORAGE_KEY);
            }
          }
        })
        .catch(() => window.sessionStorage.removeItem(JOB_STORAGE_KEY));
    }
    Promise.all([modelApi.catalog('cn', false), modelApi.catalog('global', false)])
      .then(([cn, global]) => {
        if (disposed) return;
        const rows = mergeProbeModels(cn.models, global.models);
        setModelRows(rows);
        setSelectedModels(rows.map((item) => item.name));
        if (!rows.length) setCatalogError(t('accounts.probeMatrixNoModels'));
      })
      .catch((error) => {
        if (!disposed) setCatalogError(errText(error));
      })
      .finally(() => {
        if (!disposed) setCatalogLoading(false);
      });
    return () => {
      disposed = true;
    };
    // accounts 是页面心跳产生的新数组；只在弹窗打开时重新初始化选择。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    if (!open || !currentJobId || !running) return;
    let disposed = false;
    let inFlight = false;
    let keepPolling = true;
    let timer: number | undefined;

    const poll = async () => {
      if (disposed || inFlight || !keepPolling) return;
      inFlight = true;
      try {
        const next = await accountApi.probeMatrix(currentJobId);
        if (!disposed) {
          setJob(next);
          const decision = probePollDecision(next);
          if (decision !== 'continue') {
            keepPolling = false;
            window.sessionStorage.removeItem(JOB_STORAGE_KEY);
          }
        }
      } catch (error) {
        if (!disposed) {
          const decision = probePollDecision(null, error);
          if (decision === 'lost') {
            // 服务重启或任务已被替换：404 不应把弹窗永久锁在 running。
            keepPolling = false;
            setJob(null);
            setStartBusy(false);
            window.sessionStorage.removeItem(JOB_STORAGE_KEY);
            notify.warn(errText(error));
          } else if (decision === 'denied') {
            // 401/403 不会自行恢复；继续轮询只会每 850ms 打一次无效请求。
            keepPolling = false;
            setJob(null);
            setStartBusy(false);
            window.sessionStorage.removeItem(JOB_STORAGE_KEY);
            notify.err(errText(error));
          } else {
            setCatalogError(errText(error));
          }
        }
      } finally {
        inFlight = false;
        if (!disposed && keepPolling) {
          // 递归定时：上一请求结束后才安排下一轮，不存在 850ms 窗口重叠。
          timer = window.setTimeout(() => void poll(), 850);
        }
      }
    };
    timer = window.setTimeout(() => void poll(), 850);
    return () => {
      disposed = true;
      keepPolling = false;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [open, currentJobId, running]);

  async function start() {
    // 双重守卫：按钮 disabled 防鼠标重复点击，函数入口防回车/脚本重复触发。
    if (!canStartProbe(
      startBusy, running, selectedFiles.length, selectedModels.length)) return;
    setStartBusy(true);
    setCatalogError('');
    try {
      const next = await accountApi.startProbeMatrix({
        files: selectedFiles,
        models: selectedModels,
        upstream_id: upstreamId,
        account_concurrency: accountConcurrency,
        request_interval_ms: 500,
      });
      setJob(next);
      window.sessionStorage.setItem(JOB_STORAGE_KEY, next.job_id);
    } catch (error) {
      notify.err(errText(error));
    } finally {
      setStartBusy(false);
    }
  }

  async function cancel() {
    if (!job || !running || cancelBusy) return;
    setCancelBusy(true);
    try {
      const next = await accountApi.cancelProbeMatrix(job.job_id);
      setJob(next);
      window.sessionStorage.removeItem(JOB_STORAGE_KEY);
    } catch (error) {
      notify.err(errText(error));
    } finally {
      setCancelBusy(false);
    }
  }

  function changeOpen(next: boolean) {
    if (!next && running) {
      notify.info(t('accounts.probeMatrixCloseRunning'));
      return;
    }
    onOpenChange(next);
  }

  const matrixAccounts = job
    ? job.accounts
    : accounts.filter((item) => selectedFiles.includes(item.file));
  const matrixModels = modelRows.filter((item) =>
    (job ? job.models : selectedModels).includes(item.name));

  function statusBadge(status: ProbeCellStatus, code: number | string | null,
                       message: string, elapsed: number) {
    return (
      <span
        className={'inline-flex items-center gap-1 text-[11px] font-medium ' +
          STATUS_CLASS[status]}
        title={[
          message,
          code == null ? '' : t('accounts.probeMatrixCode', {code: String(code)}),
          elapsed ? t('accounts.probeMatrixElapsed', {ms: elapsed}) : '',
        ].filter(Boolean).join(String.fromCharCode(10))}
      >
        {status === 'running' && <Loader2 className="size-3 animate-spin" />}
        {t(STATUS_KEY[status])}
      </span>
    );
  }

  return (
    <Dialog open={open} onOpenChange={changeOpen}>
      <DialogContent className="max-w-[min(1180px,calc(100vw-2rem))]" showCloseButton>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Zap className="size-4 text-muted-foreground" />
            {t('accounts.probeMatrixTitle')}
            {running && (
              <Badge variant="secondary" className="rounded-full text-[10px]">
                <Loader2 className="mr-1 size-3 animate-spin" />
                {t('accounts.probeMatrixRunning')}
              </Badge>
            )}
          </DialogTitle>
          <DialogDescription>
            {t('accounts.probeMatrixDesc')}
          </DialogDescription>
        </DialogHeader>

        <DialogBody className="max-h-[min(760px,calc(100dvh-11rem))]">
          <div className="space-y-4 px-6 pb-3">
            {catalogError && (
              <div className="flex items-start gap-2 rounded-xl bg-amber-500/10 px-3 py-2 text-[12px]">
                <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-amber-500" />
                <span>{catalogError}</span>
              </div>
            )}
            {job?.error && (
              <div className="flex items-start gap-2 rounded-xl bg-rose-500/10 px-3 py-2 text-[12px] text-rose-700 dark:text-rose-300">
                <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
                <span>{job.error}</span>
              </div>
            )}

            <div className="grid gap-3 md:grid-cols-2">
              <section className="min-w-0 rounded-xl border p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span className="text-[12px] font-medium">
                    {t('accounts.probeMatrixAccounts')}
                    <span className="ml-1 text-muted-foreground">{selectedFiles.length}/{accounts.length}</span>
                  </span>
                  <div className="flex gap-1">
                    <Button type="button" size="sm" variant="ghost" className="h-7 rounded-full text-[11px]"
                      disabled={running}
                      onClick={() => setSelectedFiles(accounts.map((item) => item.file))}>
                      {t('accounts.probeMatrixSelectAll')}
                    </Button>
                    <Button type="button" size="sm" variant="ghost" className="h-7 rounded-full text-[11px]"
                      disabled={running} onClick={() => setSelectedFiles([])}>
                      {t('accounts.probeMatrixClear')}
                    </Button>
                  </div>
                </div>
                <div className="max-h-40 space-y-1 overflow-auto pr-1">
                  {accounts.map((account) => (
                    <label key={account.file}
                      className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-[12px] hover:bg-muted">
                      <input
                        type="checkbox"
                        className="size-3.5 accent-primary"
                        checked={selectedFiles.includes(account.file)}
                        disabled={running}
                        onChange={() => setSelectedFiles((items) =>
                          toggleValue(items, account.file))}
                      />
                      <span className="min-w-0 flex-1 truncate">{account.nickname || account.uid}</span>
                      <Badge variant="secondary" className="rounded-full px-1.5 py-0 text-[9px]">
                        {account.realm ?? 'cn'}
                      </Badge>
                    </label>
                  ))}
                  {!accounts.length && (
                    <div className="py-5 text-center text-[11px] text-muted-foreground">
                      {t('accounts.probeMatrixNoAccounts')}
                    </div>
                  )}
                </div>
              </section>

              <section className="min-w-0 rounded-xl border p-3">
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span className="text-[12px] font-medium">
                    {t('accounts.probeMatrixModels')}
                    <span className="ml-1 text-muted-foreground">{selectedModels.length}/{modelRows.length}</span>
                  </span>
                  <div className="flex gap-1">
                    <Button type="button" size="sm" variant="ghost" className="h-7 rounded-full text-[11px]"
                      disabled={running || catalogLoading}
                      onClick={() => setSelectedModels(modelRows.map((item) => item.name))}>
                      {t('accounts.probeMatrixSelectAll')}
                    </Button>
                    <Button type="button" size="sm" variant="ghost" className="h-7 rounded-full text-[11px]"
                      disabled={running} onClick={() => setSelectedModels([])}>
                      {t('accounts.probeMatrixClear')}
                    </Button>
                  </div>
                </div>
                <div className="max-h-40 space-y-1 overflow-auto pr-1">
                  {modelRows.map((model) => (
                    <label key={model.name}
                      className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-[12px] hover:bg-muted">
                      <input
                        type="checkbox"
                        className="size-3.5 accent-primary"
                        checked={selectedModels.includes(model.name)}
                        disabled={running}
                        onChange={() => setSelectedModels((items) =>
                          toggleValue(items, model.name))}
                      />
                      <span className="min-w-0 flex-1 truncate" title={model.displayName}>
                        {model.displayName}
                        {model.displayName !== model.name && (
                          <span className="ml-1 font-mono text-[10px] text-muted-foreground">{model.name}</span>
                        )}
                      </span>
                      {model.realms.map((realm) => (
                        <Badge key={realm} variant="secondary"
                          className="rounded-full px-1.5 py-0 text-[9px]">
                          {realm}
                        </Badge>
                      ))}
                    </label>
                  ))}
                  {catalogLoading && (
                    <div className="flex items-center justify-center py-5 text-[11px] text-muted-foreground">
                      <Loader2 className="mr-1.5 size-3.5 animate-spin" />
                      {t('accounts.probeMatrixLoadingModels')}
                    </div>
                  )}
                </div>
              </section>
            </div>

            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl bg-muted/60 px-3 py-2.5 text-[12px]">
              <span className="font-medium">
                {t('accounts.probeMatrixRequests', {n: stats.requests})}
              </span>
              <span className="text-muted-foreground">
                {t('accounts.probeMatrixMinimumWait', {seconds: stats.estimatedSeconds})}
              </span>
              <span className="text-muted-foreground">
                {t('accounts.probeMatrixInterval')}
              </span>
              <span className="flex items-center gap-1 text-muted-foreground">
                {t('accounts.probeMatrixConcurrency')}
                {[1, 2, 3, 4].map((n) => (
                  <button
                    key={n}
                    type="button"
                    disabled={running}
                    onClick={() => setAccountConcurrency(n)}
                    className={'rounded-full px-2 py-0.5 text-[11px] ' +
                      (accountConcurrency === n
                        ? 'bg-background text-foreground shadow-sm'
                        : 'hover:bg-background/70')}
                  >
                    {n}
                  </button>
                ))}
              </span>
            </div>
            {selectionLimit && (
              <div className="rounded-xl bg-amber-500/10 px-3 py-2 text-[11px] text-amber-700 dark:text-amber-300">
                {selectionLimit === 'accounts'
                  ? t('accounts.probeMatrixLimitAccounts')
                  : selectionLimit === 'models'
                    ? t('accounts.probeMatrixLimitModels')
                    : t('accounts.probeMatrixLimit')}
              </div>
            )}

            {job && (
              <>
                <div className="flex items-center justify-between gap-3 text-[11px] text-muted-foreground">
                  <div className="flex-1">
                    <div className="mb-1 flex justify-between">
                      <span>{t('accounts.probeMatrixProgress', {
                        done: job.progress.completed,
                        total: job.progress.total,
                        ok: job.progress.succeeded,
                      })}</span>
                      <span>{t(JOB_STATE_KEY[job.state])}</span>
                    </div>
                    <div className="mb-1 text-[10px]">
                      {t('accounts.probeMatrixOutboundUsage', {
                        used: job.requests_used,
                        limit: job.request_budget,
                      })}
                    </div>
                    <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                      <div className="h-full rounded-full bg-primary transition-all"
                        style={{
                          width: `${job.progress.total
                            ? Math.round(job.progress.completed / job.progress.total * 100)
                            : 0}%`,
                        }} />
                    </div>
                  </div>
                </div>

                <div className="max-h-[330px] overflow-auto rounded-xl border">
                  <table className="min-w-full border-collapse text-left text-[11px]">
                    <thead className="sticky top-0 z-10 bg-muted">
                      <tr>
                        <th className="min-w-[180px] border-b px-3 py-2 font-medium">
                          {t('accounts.probeMatrixModel')}
                        </th>
                        {matrixAccounts.map((account) => (
                          <th key={`${account.realm}:${account.file}`}
                            className="min-w-[145px] border-b border-l px-3 py-2 font-medium">
                            <div className="max-w-[180px] truncate">{account.nickname}</div>
                            <div className="text-[9px] font-normal text-muted-foreground">
                              {account.realm}
                            </div>
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {matrixModels.map((model) => (
                        <tr key={model.name} className="border-b last:border-b-0">
                          <th className="bg-background/95 px-3 py-2 align-top font-medium">
                            <div className="max-w-[240px] truncate" title={model.name}>{model.name}</div>
                            <div className="mt-0.5 flex gap-1">
                              {model.realms.map((realm: ProbeRealm) => (
                                <span key={realm}
                                  className="rounded-full bg-muted px-1.5 py-0 text-[9px] font-normal text-muted-foreground">
                                  {realm}
                                </span>
                              ))}
                            </div>
                          </th>
                          {matrixAccounts.map((account) => {
                            const realm = account.realm as ProbeRealm;
                            const item = probeCellResult(
                              job.results, account.file, realm, model.name);
                            return (
                              <td key={`${account.realm}:${account.file}`}
                                className="border-l px-3 py-2 align-top">
                                {item
                                  ? statusBadge(item.status, item.code, item.message, item.elapsed_ms)
                                  : <span className="text-muted-foreground">—</span>}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>
        </DialogBody>

        <DialogFooter>
          {running ? (
            <Button variant="outline" className="rounded-full"
              disabled={cancelBusy} onClick={cancel}>
              {cancelBusy ? <Loader2 className="animate-spin" /> : <Square />}
              {t('accounts.probeMatrixCancel')}
            </Button>
          ) : (
            <Button className="rounded-full"
              disabled={!canStartProbe(
                startBusy, running, selectedFiles.length, selectedModels.length)}
              onClick={start}>
              {startBusy ? <Loader2 className="animate-spin" /> : <Play />}
              {t('accounts.probeMatrixStart')}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
