import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/gateway'
const columns = [
  "配置编号",
  "网关名称",
  "监听端口",
  "上一版监听端口",
  "协议类型",
  "认证方式",
  "超时秒数",
  "并发上限",
  "完成位置",
  "配置状态"
]
const actions = [
  "启动网关",
  "暂停网关",
  "关闭网关"
]
const statuses = [
  "草稿",
  "运行中",
  "已暂停",
  "已关闭"
]
const batchStatusTone: Record<string, string> = {
  已生效: 'ok',
  已跳过: 'muted',
  已回退: 'error',
  待生效: 'pending',
}

type Row = Record<string, string | number | null>
type PortOption = { 端口: number; 可用: boolean; 占用网关: string | null }
type GatewayOption = {
  id: number
  配置编号: string
  网关名称: string
  当前监听端口: number
  当前并发上限: number
  上一版监听端口: number
  完成位置: string | null
  运行状态: string
  依赖实例: string[]
}
type PortOptions = { ports: PortOption[]; gateways: GatewayOption[] }
type BatchDraft = {
  配置编号: string
  监听端口: number | null
  并发上限: number | null
}
type BatchResult = {
  batch_id: string
  执行序号: number
  批次总数: number
  配置编号: string
  网关名称: string
  状态: string
  消息: string
  依赖实例: string[]
  变更前监听端口: number
  目标监听端口: number
  变更前并发上限: number
  目标并发上限: number
  上一版监听端口: number
  已完成位置: number | null
  完成位置: string | null
  旧端口连接保留: number
  新端口连接关闭: number
  路由同步: {
    状态: string
    规则: { 规则编号: string; 目标服务: string; 同步前端口: number; 同步后端口: number; 同步状态: string }[]
    说明: string
  }
}
type SwitchResponse = {
  ok: boolean
  message: string
  results: BatchResult[]
  totals: Record<string, number>
}
type Connection = {
  连接编号: string
  配置编号: string
  实例编号: string
  端口: number
  状态: string
  批次编号: string
  备注: string
}
type PlanSummary = {
  batch_id: string
  执行序号: number
  配置编号: string
  状态: string
  消息: string
  '变更前': Record<string, number | string | null>
  '变更后': Record<string, number | string | null>
  完成时间: string | null
}

export default function GatewayView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const [options, setOptions] = useState<PortOptions>({ ports: [], gateways: [] })
  const [selectedPorts, setSelectedPorts] = useState<number[]>([])
  const [drafts, setDrafts] = useState<BatchDraft[]>([])
  const [drainSeconds, setDrainSeconds] = useState(30)
  const [switchResults, setSwitchResults] = useState<BatchResult[]>([])
  const [switchMessage, setSwitchMessage] = useState('')
  const [switchTotals, setSwitchTotals] = useState<Record<string, number>>({})
  const [plans, setPlans] = useState<PlanSummary[]>([])
  const [connections, setConnections] = useState<Connection[]>([])
  const [busy, setBusy] = useState(false)

  const stats = [
    { label: '网关配置总数', value: total },
    { label: '运行中网关', value: options.gateways.filter((item) => item.运行状态 === '运行中').length },
    { label: '已暂停网关', value: options.gateways.filter((item) => item.运行状态 === '已暂停').length },
  ]

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('网关配置列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '接入网关管理列表读取失败')
    }
  }, [filters])

  const reloadBatches = useCallback(async () => {
    try {
      const [optionsResponse, plansResponse, connectionsResponse] = await Promise.all([
        request(`${ENDPOINT}/ports`),
        request(`${ENDPOINT}/batches?size=20`),
        request(`${ENDPOINT}/connections`),
      ])
      if (optionsResponse.ok) {
        const payload = await optionsResponse.json()
        setOptions(payload)
        setDrafts((prev) => {
          const existing = new Map(prev.map((item) => [item.配置编号, item]))
          return (payload.gateways ?? []).map((gateway: GatewayOption) => existing.get(gateway.配置编号) ?? {
            配置编号: gateway.配置编号,
            监听端口: null,
            并发上限: null,
          })
        })
      }
      if (plansResponse.ok) {
        const payload = await plansResponse.json()
        setPlans(payload.items ?? [])
      }
      if (connectionsResponse.ok) {
        const payload = await connectionsResponse.json()
        setConnections(payload.items ?? [])
      }
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '批次数据读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void reloadBatches()
  }, [reloadBatches])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('接入网关管理动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '接入网关管理操作失败')
    }
  }

  function togglePort(port: number) {
    setSelectedPorts((prev) => (prev.includes(port) ? prev.filter((item) => item !== port) : [...prev, port]))
  }

  function applySelectedPorts() {
    if (selectedPorts.length === 0) {
      setErrorMessage('请先在候选端口里多选至少一个监听端口')
      return
    }
    setErrorMessage('')
    setDrafts((prev) =>
      prev.map((item, index) =>
        index < selectedPorts.length ? { ...item, 监听端口: selectedPorts[index] } : item,
      ),
    )
  }

  function updateDraft(configNo: string, patch: Partial<BatchDraft>) {
    setDrafts((prev) => prev.map((item) => (item.配置编号 === configNo ? { ...item, ...patch } : item)))
  }

  const readyBatches = drafts.filter((item) =>
    item.监听端口 !== null || item.并发上限 !== null,
  )

  async function submitBatches() {
    setErrorMessage('')
    if (readyBatches.length === 0) {
      setErrorMessage('请至少为一个网关选择目标监听端口或填写并发上限')
      return
    }
    setBusy(true)
    try {
      const response = await request(`${ENDPOINT}/batches`, {
        method: 'POST',
        body: JSON.stringify({
          batches: readyBatches.map((item) => ({
            配置编号: item.配置编号,
            监听端口: item.监听端口,
            并发上限: item.并发上限,
          })),
          drain_seconds: drainSeconds,
        }),
      })
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(payload?.detail ?? '批量切换未执行')
      }
      setSwitchResults(payload.results ?? [])
      setSwitchMessage(payload.message ?? '')
      setSwitchTotals(payload.totals ?? {})
      await Promise.all([reload(), reloadBatches()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '批量切换失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="page" data-module="gateway">
      <header className="page-head">
        <div>
          <h2>接入网关管理管理</h2>
          <p className="page-desc">维护接入网关的端口配置、协议选择与认证方式，为接入网关管理提供可审计的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('网关配置登记入口尚未接入审批流')}>
            登记网关配置
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出接入网关管理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无接入网关管理数据，可先登记网关配置</td>
            </tr>
          )}
        </tbody>
      </table>

      <section className="panel" aria-label="批量切换监听端口与并发上限">
        <header className="panel-head">
          <div>
            <h3>批量切换监听端口与并发上限</h3>
            <p className="page-desc">按网关配置与注册实例依赖逐批生效；同一端口只有首个通过校验的批次生效，失败批次只回退自己。</p>
          </div>
          <label className="inline-field">
            <span>连接排空秒数</span>
            <input
              type="number"
              min={0}
              max={600}
              value={drainSeconds}
              onChange={(event) => setDrainSeconds(Number(event.target.value))}
            />
          </label>
        </header>

        <div className="port-row">
          <span className="port-label">多选监听端口</span>
          {options.ports.map((item) => (
            <button
              key={item.端口}
              type="button"
              className={`chip ${selectedPorts.includes(item.端口) ? 'chip-active' : ''} ${item.可用 ? '' : 'chip-disabled'}`}
              onClick={() => togglePort(item.端口)}
              title={item.可用 ? '空闲端口' : `已被 ${item.占用网关 ?? '其他网关'} 占用`}
            >
              {item.端口}{item.可用 ? '' : '（占用）'}
            </button>
          ))}
          <button className="btn ghost" type="button" onClick={applySelectedPorts}>
            按顺序填充到各批
          </button>
          <button className="btn ghost" type="button" onClick={() => setSelectedPorts([])}>清空多选</button>
        </div>

        <table className="data-table compact">
          <thead>
            <tr>
              <th>配置编号</th>
              <th>网关名称</th>
              <th>依赖实例</th>
              <th>当前端口</th>
              <th>目标端口</th>
              <th>并发上限</th>
              <th>完成位置</th>
            </tr>
          </thead>
          <tbody>
            {drafts.map((draft) => {
              const gateway = options.gateways.find((item) => item.配置编号 === draft.配置编号)
              return (
                <tr key={draft.配置编号}>
                  <td>{draft.配置编号}</td>
                  <td>{gateway?.网关名称 ?? '—'}</td>
                  <td>{gateway?.依赖实例?.join('、') || '—'}</td>
                  <td>{gateway?.当前监听端口 ?? '—'}</td>
                  <td>
                    <select
                      value={draft.监听端口 ?? ''}
                      onChange={(event) => updateDraft(draft.配置编号, {
                        监听端口: event.target.value ? Number(event.target.value) : null,
                      })}
                    >
                      <option value="">保持不变</option>
                      {options.ports.map((item) => (
                        <option key={item.端口} value={item.端口}>
                          {item.端口}{item.可用 ? '' : '（占用）'}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <input
                      type="number"
                      min={1}
                      placeholder={String(gateway?.当前并发上限 ?? '')}
                      value={draft.并发上限 ?? ''}
                      onChange={(event) => updateDraft(draft.配置编号, {
                        并发上限: event.target.value ? Number(event.target.value) : null,
                      })}
                    />
                  </td>
                  <td>{gateway?.完成位置 ?? '未执行'}</td>
                </tr>
              )
            })}
            {drafts.length === 0 && (
              <tr>
                <td className="empty-state" colSpan={7}>暂无网关配置</td>
              </tr>
            )}
          </tbody>
        </table>

        <div className="panel-actions">
          <button className="btn primary" type="button" disabled={busy} onClick={() => void submitBatches()}>
            {busy ? '正在逐批生效…' : `提交 ${readyBatches.length} 个网关批次`}
          </button>
          {switchMessage && <span className="panel-message">{switchMessage}</span>}
        </div>

        {switchResults.length > 0 && (
          <table className="data-table compact">
            <thead>
              <tr>
                <th>执行序号</th>
                <th>批次编号</th>
                <th>配置编号</th>
                <th>状态</th>
                <th>变更前端口</th>
                <th>目标端口</th>
                <th>连接</th>
                <th>路由同步</th>
                <th>说明</th>
              </tr>
            </thead>
            <tbody>
              {switchResults.map((item) => (
                <tr key={item.batch_id}>
                  <td>{item.执行序号}/{item.批次总数}</td>
                  <td>{item.batch_id}</td>
                  <td>{item.配置编号}</td>
                  <td><span className={`status-pill ${batchStatusTone[item.状态] ?? ''}`}>{item.状态}</span></td>
                  <td>{item.变更前监听端口 ?? '—'}</td>
                  <td>{item.目标监听端口 ?? '—'}</td>
                  <td>保留旧端口 {item.旧端口连接保留} / 关闭新端口 {item.新端口连接关闭}</td>
                  <td>{item.路由同步?.状态 ?? '—'}</td>
                  <td className="message-cell">{item.消息}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {Object.keys(switchTotals).length > 0 && (
          <p className="panel-message">
            已生效 {switchTotals['已生效'] ?? 0} 批，已跳过 {switchTotals['已跳过'] ?? 0} 批，已回退 {switchTotals['已回退'] ?? 0} 批
          </p>
        )}
      </section>

      <section className="panel" aria-label="批次接入计划">
        <header className="panel-head">
          <div>
            <h3>批次接入计划与连接排空记录</h3>
            <p className="page-desc">每批保存变更前的网关运行状态、完成位置和上一版端口；失败批次保留旧端口连接。</p>
          </div>
        </header>
        <table className="data-table compact">
          <thead>
            <tr>
              <th>批次编号</th>
              <th>执行序号</th>
              <th>配置编号</th>
              <th>状态</th>
              <th>变更前端口</th>
              <th>变更后端口</th>
              <th>完成时间</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            {plans.map((plan) => (
              <tr key={plan.batch_id}>
                <td>{plan.batch_id}</td>
                <td>{plan.执行序号}</td>
                <td>{plan.配置编号}</td>
                <td><span className={`status-pill ${batchStatusTone[plan.状态] ?? ''}`}>{plan.状态}</span></td>
                <td>{plan['变更前']?.['监听端口'] ?? '—'}</td>
                <td>{plan['变更后']?.['监听端口'] ?? '—'}</td>
                <td>{plan.完成时间 ?? '—'}</td>
                <td className="message-cell">{plan.消息}</td>
              </tr>
            ))}
            {plans.length === 0 && (
              <tr>
                <td className="empty-state" colSpan={8}>暂无批次计划</td>
              </tr>
            )}
          </tbody>
        </table>

        {connections.length > 0 && (
          <table className="data-table compact">
            <thead>
              <tr>
                <th>连接编号</th>
                <th>配置编号</th>
                <th>实例编号</th>
                <th>端口</th>
                <th>状态</th>
                <th>批次编号</th>
                <th>备注</th>
              </tr>
            </thead>
            <tbody>
              {connections.map((connection) => (
                <tr key={connection.连接编号}>
                  <td>{connection.连接编号}</td>
                  <td>{connection.配置编号}</td>
                  <td>{connection.实例编号}</td>
                  <td>{connection.端口}</td>
                  <td><span className={`status-pill ${connection.状态 === '已关闭' ? 'error' : connection.状态 === '排空中' ? 'pending' : 'ok'}`}>{connection.状态}</span></td>
                  <td>{connection.批次编号}</td>
                  <td className="message-cell">{connection.备注}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <footer className="page-foot">
        <span>共 {total} 条接入网关管理记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
