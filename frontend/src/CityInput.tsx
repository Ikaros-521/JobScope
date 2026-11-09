import { useEffect, useId, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'

// 采集侧只认来源字典里的城市名，提示把这些名字摆出来；别名表只服务拼音输入，缺别名的新城市仍可按中文名命中。
const pinyin: Record<string, [string, string]> = {
  北京: ['beijing', 'bj'], 上海: ['shanghai', 'sh'], 广州: ['guangzhou', 'gz'], 深圳: ['shenzhen', 'sz'],
  杭州: ['hangzhou', 'hz'], 成都: ['chengdu', 'cd'], 武汉: ['wuhan', 'wh'], 南京: ['nanjing', 'nj'],
  苏州: ['suzhou', 'sz'], 西安: ['xian', 'xa'], 全国: ['quanguo', 'qg'],
}

function suggestCities(cities: string[], term: string): string[] {
  const text = term.trim()
  if (!text) return cities
  const latin = /^[A-Za-z]+$/.test(text)
  const key = text.toLowerCase()
  return cities
    .map(city => {
      const [full, initials] = pinyin[city] || ['', '']
      if (latin) return { city, score: full.startsWith(key) ? 0 : initials.startsWith(key) ? 1 : -1 }
      if (city.startsWith(text) || text.startsWith(city)) return { city, score: 0 }
      return { city, score: city.includes(text) ? 1 : -1 }
    })
    .filter(item => item.score >= 0)
    .sort((a, b) => a.score - b.score)
    .map(item => item.city)
}

export function CityInput({ cities, value, onChange, placeholder, maxLength, id, label }: {
  cities: string[]
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
  const items = useMemo(() => suggestCities(cities, typed ? value : ''), [cities, typed, value])
  const shown = open && cities.length > 0

  useEffect(() => { setActive(0) }, [items, open])
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
      if (event.key === 'ArrowDown' && cities.length) { event.preventDefault(); setOpen(true) }
      return
    }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      if (!items.length) return
      setActive(index => event.key === 'ArrowDown' ? (index + 1) % items.length : (index - 1 + items.length) % items.length)
    } else if (event.key === 'Escape') {
      event.stopPropagation()
      setOpen(false)
    } else if (event.key === 'Enter' && items[active]) {
      event.preventDefault()
      choose(items[active])
    }
  }

  return <div className="city-input">
    <span className="sr-only" id={hintId}>可用汉字、拼音或首字母，例如 杭州、hangzhou、hz</span>
    <input id={id} value={value} placeholder={placeholder} maxLength={maxLength} autoComplete="off" aria-label={label} aria-describedby={hintId}
      role="combobox" aria-expanded={shown} aria-controls={listId} aria-autocomplete="list"
      aria-activedescendant={shown && items[active] ? `${listId}-${active}` : undefined}
      onChange={event => { setTyped(true); onChange(event.target.value) }}
      onFocus={() => { setTyped(false); setOpen(true) }}
      onBlur={() => setOpen(false)}
      onKeyDown={onKeyDown} />
    {shown && <div className="city-suggestions" role="listbox" id={listId} aria-label="候选城市" ref={panel}>
      {items.length ? items.map((city, index) => <button type="button" role="option" id={`${listId}-${index}`} key={city}
        aria-selected={index === active} className={index === active ? 'active' : ''}
        onMouseDown={event => event.preventDefault()} onClick={() => choose(city)}>
        <span>{city}</span>{pinyin[city] && <em>{pinyin[city][0]}</em>}
      </button>) : <div className="city-empty">该来源暂未映射这个城市；可继续输入，只用本地档案筛选</div>}
    </div>}
  </div>
}
