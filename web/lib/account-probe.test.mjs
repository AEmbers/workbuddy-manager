import {
  canStartProbe,
  isProbeJobActive,
  mergeProbeModels,
  probeCellResult,
  probePollDecision,
  probeSelectionLimit,
  probeSelectionStats,
} from './account-probe.ts';

let failed = 0;

function check(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g === w) {
    console.log(`  ok  ${name}`);
  } else {
    console.log(`  FAIL ${name}\n       got  ${g}\n       want ${w}`);
    failed += 1;
  }
}

const cn = [
  {id: 'cn:glm-5.2', name: 'GLM 5.2'},
  {id: 'cn:deepseek-v4.1-flash', name: 'DeepSeek Flash'},
];
const global = [
  {id: 'global:glm-5.2', name: 'GLM 5.2 Global'},
  {id: 'global:kimi-k2.8-preview', name: 'Kimi'},
];

check('同名 cn/global 合并一行但保留双 realm', mergeProbeModels(cn, global), [
  {name: 'glm-5.2', displayName: 'GLM 5.2', realms: ['cn', 'global']},
  {name: 'deepseek-v4.1-flash', displayName: 'DeepSeek Flash', realms: ['cn']},
  {name: 'kimi-k2.8-preview', displayName: 'Kimi', realms: ['global']},
]);

check('请求数与最低等待按账号并发波次计算', probeSelectionStats(5, 4, 2), {
  requests: 20,
  accountWaves: 3,
  estimatedSeconds: 6,
});

check('空选择不得启动', canStartProbe(false, false, 0, 3), false);
check('忙碌/已有任务时不得重复启动', [
  canStartProbe(true, false, 2, 3),
  canStartProbe(false, true, 2, 3),
], [false, false]);
check('有效选择可启动', canStartProbe(false, false, 2, 3), true);
check('超过后端单次请求上限不得启动',
  canStartProbe(false, false, 50, 21), false);
check('选择上限能解释按钮为何禁用', [
  probeSelectionLimit(101, 1),
  probeSelectionLimit(1, 51),
  probeSelectionLimit(50, 21),
  probeSelectionLimit(50, 20),
], ['accounts', 'models', 'requests', null]);

check('活动任务状态机继续轮询', [
  isProbeJobActive({state: 'queued'}),
  isProbeJobActive({state: 'running'}),
  isProbeJobActive({state: 'cancelling'}),
], [true, true, true]);
check('所有终态停止轮询', [
  probePollDecision({state: 'finished'}),
  probePollDecision({state: 'cancelled'}),
  probePollDecision({state: 'failed'}),
], ['terminal', 'terminal', 'terminal']);
check('404 丢失，401/403 停止，其他轮询错误可重试', [
  probePollDecision(null, {response: {status: 404}}),
  probePollDecision(null, {response: {status: 401}}),
  probePollDecision(null, {response: {status: 403}}),
  probePollDecision(null, {response: {status: 502}}),
], ['lost', 'denied', 'denied', 'retry']);

const results = [
  {account_file: 'a.json', realm: 'cn', model: 'glm-5.2', status: 'success'},
  {account_file: 'a.json', realm: 'global', model: 'glm-5.2', status: 'unauthorized'},
];
check('同账号同名模型的状态按 realm 分开取', [
  probeCellResult(results, 'a.json', 'cn', 'glm-5.2')?.status,
  probeCellResult(results, 'a.json', 'global', 'glm-5.2')?.status,
], ['success', 'unauthorized']);

if (failed) {
  console.log(`\n${failed} failed`);
  process.exit(1);
}
console.log('\nall passed');
