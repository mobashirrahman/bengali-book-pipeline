import { useState } from 'react'

interface RatingControlProps {
  value: number
  onChange: (value: number) => void | Promise<void>
  onClear?: () => void | Promise<void>
  disabled?: boolean
  note?: string
}

export function RatingControl({ value, onChange, onClear, disabled = false, note = 'Sign in to sync' }: RatingControlProps) {
  const [focused, setFocused] = useState(0)
  const current = focused || value
  return (
    <fieldset className="rating-control" disabled={disabled}>
      <legend>Your rating</legend>
      <div className="rating-control__stars" onMouseLeave={() => setFocused(0)}>
        {[1, 2, 3, 4, 5].map((star) => <button key={star} type="button" className={star <= current ? 'is-active' : ''} aria-label={`${star} out of 5`} aria-pressed={value === star} onFocus={() => setFocused(star)} onMouseEnter={() => setFocused(star)} onClick={() => void onChange(star)}>★</button>)}
      </div>
      <span className="rating-control__note">{note}</span>
      {onClear && value > 0 && <button className="rating-control__clear" type="button" onClick={() => void onClear()}>Clear</button>}
    </fieldset>
  )
}
