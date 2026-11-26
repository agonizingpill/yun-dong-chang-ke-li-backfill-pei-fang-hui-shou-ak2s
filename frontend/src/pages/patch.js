import {el} from '../dom.js';
import {mill} from '../mill-api.js';
import {desk} from '../mill-state.js';
import {actionForm, input, kilograms, mayWrite, select} from '../weights.js';
import {dataView, status, titleBlock} from '../widgets.js';

const coordinates = [...'ABCD'].flatMap(row => Array.from({length:8}, (_, column) => row + (column + 1)));
const points = [
  ['first_mm','起点（mm）'], ['middle_mm','中点（mm）'], ['last_mm','终点（mm）'],
];
const stage = p => p.opened_by ? '已签开场' : p.laid_by ? '已实铺' : '待实铺';
const latestRoutine = cell => cell.latest_routine ?? null;
const mm = value => value === null || value === undefined ? '—' : Number(value).toFixed(1) + ' mm';

function condition(cell) {
  if (cell.open_seam_review) return '待接缝复核';
  const routine = latestRoutine(cell);
  if (routine === null) return '待测';
  return routine.passed ? '合格' : '待复测';
}

function planner(board, preferred, complete) {
  if (!board.eligible_tickets.length) return status('有已发料的称量单后可划定补坪范围。');
  const ticketSelect = select('ticket','已发料单',board.eligible_tickets.map(ticket =>
    [ticket.code, ticket.code + ' · ' + ticket.zone_code + ' · ' + kilograms(ticket.total_g)]));
  const control = ticketSelect.querySelector('select');
  if (board.eligible_tickets.some(ticket => ticket.code === preferred)) control.value = preferred;
  const grid = el('fieldset',{class:'patch-plan','aria-label':'待补坪网格'});
  function layout() {
    const ticket = board.eligible_tickets.find(item => item.code === control.value);
    const taken = new Set(board.active_cells.filter(c => c.zone_code === ticket.zone_code).map(c => c.grid_ref));
    grid.replaceChildren(el('legend',{},ticket.zone_code + ' · 选 1–8 格'),
      ...coordinates.map(ref => el('label',{class:'patch-choice'},
        el('input',{type:'checkbox',name:'grid_refs',value:ref,disabled:taken.has(ref) ? '' : undefined,
          'aria-label':'网格 ' + ref}),el('span',{},ref))));
  }
  control.addEventListener('change',layout);
  layout();
  return actionForm('mark-patch',[ticketSelect,grid],'划定补坪范围',async values => {
    const refs = values.getAll('grid_refs');
    if (refs.length < 1 || refs.length > 8) throw new Error('请选择 1–8 个可用网格。');
    const code = values.get('ticket');
    await mill.markPatch(code,{grid_refs:refs});
    await complete(code);
  });
}

function cellHistory(cell) {
  const rows = [...cell.history].reverse().map(r => ({
    id:r.id, kind:r.kind === 'seam_review' ? '接缝复核' : '常规测读',
    depth:[r.first_mm,r.middle_mm,r.last_mm].join(' / '), spread:r.spread_mm,
    outcome:r.passed ? '合格' : '未通过', prior:r.repeat_of, by:r.measured_by,
  }));
  return dataView(rows,[
    {name:'id',label:'读数号'},{name:'kind',label:'类型'},{name:'depth',label:'三点（mm）'},
    {name:'spread',label:'极差（mm）'},{name:'outcome',label:'结论'},
    {name:'prior',label:'关联读数'},{name:'by',label:'登记人'},
  ],'table');
}

function seamPanel(cell, opened) {
  const routine = latestRoutine(cell);
  const prediction = cell.seam_prediction_mm;
  if (prediction === null || prediction === undefined) {
    return el('p',{class:'seam-note'},'合格邻格不足两格，暂无接缝厚差预测。');
  }
  const observed = routine ? (routine.first_mm + routine.middle_mm + routine.last_mm) / 3 : null;
  const children = [
    el('dl',{class:'seam-figures'},
      el('dt',{},'邻格预测厚度'), el('dd',{},mm(prediction)),
      el('dt',{},'本格均值'), el('dd',{},mm(observed)),
      el('dt',{},'偏离'), el('dd',{},mm(cell.seam_deviation_mm))),
  ];
  if (cell.open_seam_review) {
    children.push(status('接缝厚差偏离超过 1 mm，须换人复核并过原标准后才能开场。','warn'));
  } else if (opened) {
    children.push(status('开场签字时已留住当时读数与预测。','ok'));
  }
  return el('div',{class:'seam-panel'},el('h4',{},'接缝厚差预测'),...children);
}

export async function patchPage(root, role) {
  const rail = el('div',{class:'cardrail'});
  const controls = el('section',{class:'ledger'});
  const panel = el('section',{class:'ledger patch-detail'});
  root.replaceChildren(titleBlock('现场 · 局部施工','补坪验收与开场',
    '按发料单划定网格。每格在起点、中点、终点测厚，三点须在 12–16 mm 内且极差不超过 2 mm。'
    + '同区上下左右合格邻格不少于两格时，用其最新常规读数三点均值的中位数预测本格；'
    + '本格虽合格但均值偏离预测超过 1 mm，须换人复核，复核读数不入预测。'),
  el('div',{class:'workspace-split'},panel,controls));
  let code = desk.activeTicket;
  let gridRef = null;
  let viewId = 0;

  function show(detail) {
    if (detail.patch.code !== code) return;
    const work = detail.patch;
    gridRef = detail.cells.some(cell => cell.grid_ref === gridRef) ? gridRef : detail.cells[0].grid_ref;
    const current = detail.cells.find(cell => cell.grid_ref === gridRef);
    const mark = el('div',{class:'patch-map','aria-label':'工单网格'},detail.cells.map(cell =>
      el('button',{class:'quiet',type:'button','data-grid':cell.grid_ref,'data-condition':condition(cell),
        'aria-pressed':String(cell === current),onclick:() => { gridRef = cell.grid_ref; show(detail); }},
      cell.grid_ref + ' · ' + condition(cell))));
    const body = el('div',{class:'patch-evidence'},
      el('h3',{},current.grid_ref + ' 测读记录'),cellHistory(current),seamPanel(current, Boolean(work.opened_by)));
    panel.replaceChildren(el('div',{class:'section-label'},work.zone_code + ' · ' + stage(work)),
      el('h2',{},work.code),el('p',{},'可开场网格 ' + detail.passed_cells + ' / ' + detail.cells.length),mark,body);
    if (work.opened_by) {
      panel.append(status('已签开场 · ' + work.opened_by,'ok'));
    } else if (!work.laid_by && mayWrite(role)) {
      panel.append(actionForm('lay-patch',[],'登记实铺',async () => show(await mill.layPatch(work.code))));
    } else if (work.laid_by) {
      const routine = latestRoutine(current);
      if (current.open_seam_review) {
        const trigger = current.open_seam_review;
        if (mayWrite(role)) {
          const fields = points.map(([name,label]) => input(name,label,{type:'number',min:'0',max:'35',step:'1'}));
          panel.append(actionForm('seam-review-patch',[
            el('p',{class:'seam-note'},'原测量人 ' + trigger.created_by
              + ' 的读数触发复核，请由另一人按 12–16 mm、极差不超过 2 mm 的原标准复测。'),
            el('div',{class:'depth-inputs'},fields)],
            '提交接缝复核',async values => {
              const readings = Object.fromEntries(points.map(([name]) => [name,Number(values.get(name))]));
              show(await mill.seamReviewPatch(work.code,current.grid_ref,readings));
            }));
        }
      } else if (mayWrite(role) && (routine === null || !routine.passed)) {
        const fields = points.map(([name,label]) => input(name,label,{type:'number',min:'0',max:'35',step:'1'}));
        panel.append(actionForm('measure-patch',[el('div',{class:'depth-inputs'},fields)],
          routine ? '提交复测' : '提交测读',async values => {
            const readings = Object.fromEntries(points.map(([name]) => [name,Number(values.get(name))]));
            show(await mill.measurePatch(work.code,current.grid_ref,
              {...readings,repeat_of:routine && !routine.passed ? routine.id : null}));
          }));
      }
      if (role === 'reviewer' && detail.passed_cells === detail.cells.length) {
        panel.append(actionForm('open-patch',[],'签开场许可',async () => {
          show(await mill.openPatch(work.code));
          await refreshBoard();
        }));
      }
    }
  }

  async function open(target) {
    if (target !== code) gridRef = null;
    code = desk.activeTicket = target;
    for (const button of rail.querySelectorAll('button')) {
      button.setAttribute('aria-pressed', String(button.textContent === target));
    }
    const currentView = ++viewId;
    const detail = await mill.patch(target);
    if (currentView === viewId) show(detail);
  }

  async function refreshBoard() {
    const board = await mill.patchBoard();
    rail.replaceChildren(...board.patches.map(work => el('button',{class:'quiet',type:'button',
      'aria-pressed':String(work.code === code),
      onclick:() => open(work.code).catch(error => panel.replaceChildren(status(error.message,'error')))},work.code)));
    controls.replaceChildren(el('h2',{},'补坪工单'),rail);
    if (mayWrite(role)) controls.append(planner(board,code,async target => {
      await refreshBoard();
      await open(target);
    }));
    return board;
  }

  const board = await refreshBoard();
  if (board.patches.some(work => work.code === code)) await open(code);
  else if (mayWrite(role) && board.eligible_tickets.some(ticket => ticket.code === code)) {
    panel.append(status('当前发料单可在右侧划定补坪网格。'));
  } else if (board.patches.length) await open(board.patches[0].code);
  else panel.append(status('暂无补坪工单。'));
}
