import { Fragment, useEffect, useId, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'

// 全拼与首字母都来自来源字典（/api/cities）：全拼由平台自身给出，首字母在抓取脚本里按读音一致性生成，前端不再自带名单。
export type SavedLocation = { location: string; jobs: number }
type Row = { name: string; note: string; group: 'collect' | 'archive' }
const groupNames: Record<Row['group'], string> = { collect: '采集可用城市', archive: '档案地点 · 只做本地筛选' }

function suggest(values: string[], term: string, pinyin: Record<string, string[]>): string[] {
  const text = term.trim()
  if (!text) return values
  const latin = /^[A-Za-z]+$/.test(text)
  const key = text.toLowerCase()
  return values
    .map(value => {
      if (latin) {
        const [full = '', short = ''] = pinyin[value] || []
        return { value, score: full.startsWith(key) ? 0 : short.startsWith(key) ? 1 : -1 }
      }
      if (value.startsWith(text) || text.startsWith(value)) return { value, score: 0 }
      return { value, score: value.includes(text) ? 1 : -1 }
    })
    .filter(item => item.score >= 0)
    .sort((a, b) => a.score - b.score)
    .map(item => item.value)
}

export function CityInput({ cities, hot, pinyin, locations, value, onChange, placeholder, maxLength, id, label }: {
  cities: string[]
  hot: string[]
  pinyin: Record<string, string[]>
  locations: SavedLocation[]
  value: string
  onChange: (value: string) => void
  placeholder?: string
  maxLength?: number
  id?: string
  label?: string
}) {
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState(false)
  const [active, setActive] = useState(0)
  const panel = useRef<HTMLDivElement>(null)
  const listId = useId()
  const hintId = useId()
  const term = typed ? value : ''
  // 空输入时只铺热门城市，全量靠输入展开；顺序照平台给的热门表，没有热门表的来源本来就十来个，全列即可。
  const pool = term || !hot.length ? cities : hot.filter(name => cities.includes(name))
  const hidden = cities.length - pool.length
  const { rows, collectLast } = useMemo(() => {
    const collect = suggest(pool, term, pinyin)
    const seen = new Set(collect)
    const archive = suggest(locations.map(item => item.location), term, pinyin).filter(name => !seen.has(name)).slice(0, 8)
    const list: Row[] = [
      ...collect.map<Row>(name => ({ name, note: (pinyin[name] || []).filter(Boolean).join(' '), group: 'collect' })),
      ...archive.map<Row>(name => ({ name, note: `${locations.find(item => item.location === name)?.jobs ?? 0} 条`, group: 'archive' })),
    ]
    return { rows: list, collectLast: list.length ? list.length - 1 - [...list].reverse().findIndex(row => row.group === 'collect') : -1 }
  }, [pool, term, pinyin, locations])
  const shown = open && (cities.length > 0 || locations.length > 0)

  useEffect(() => { setActive(0) }, [rows, open])
  useEffect(() => {
    if (shown) panel.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' })
  }, [active, shown])

  function choose(city: string) {
    onChange(city)
    setTyped(false)
    setOpen(false)
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (!shown) {
      if (event.key === 'ArrowDown' && (cities.length || locations.length)) { event.preventDefault(); setOpen(true) }
      return
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      if (!rows.length) return
      setActive(index => event.key === 'ArrowDown' ? (index + 1) % rows.length : (index - 1 + rows.length) % rows.length)
    } else if (event.key === 'Escape') {
      event.stopPropagation()
      setOpen(false)
    } else if (event.key === 'Enter' && rows[active]) {
      event.preventDefault()
      choose(rows[active].name)
    }
  }

  return <div className="city-input">
    <span className="sr-only" id={hintId}>可用汉字、拼音或首字母，例如 杭州、hangzhou、hz</span>
    <input id={id} value={value} placeholder={placeholder} maxLength={maxLength} autoComplete="off" aria-label={label} aria-describedby={hintId}
      role="combobox" aria-expanded={shown} aria-controls={listId} aria-autocomplete="list"
      aria-activedescendant={shown && rows[active] ? `${listId}-${active}` : undefined}
      onChange={event => { setTyped(true); setOpen(true); onChange(event.target.value) }}
      onFocus={() => { setTyped(false); setOpen(true) }}
      onBlur={() => setOpen(false)}
      onKeyDown={onKeyDown} />
    {shown && <div className="city-suggestions" role="listbox" id={listId} aria-label="候选地点" ref={panel}>
      {rows.length ? rows.map((row, index) => <Fragment key={row.group + row.name}>
        {(index === 0 || rows[index - 1].group !== row.group) && <div className="city-group" role="presentation">{groupNames[row.group]}</div>}
        <button type="button" role="option" id={`${listId}-${index}`} aria-selected={index === active}
          className={index === active ? 'active' : ''}
          onMouseDown={event => event.preventDefault()} onClick={() => choose(row.name)}>
          <span>{row.name}</span>{row.note && <em>{row.note}</em>}
        </button>
        {index === collectLast && hidden > 0 && !term && <div className="city-note" role="presentation">继续输入可展开另外 {hidden} 个城市</div>}
      </Fragment>) : <div className="city-empty">该来源暂未映射这个城市；可继续输入，只用本地档案筛选</div>}
    </div>}
  </div>
}
