import { useState } from 'react'

interface RatingControlProps {
  value: number
  onChange: (value: number) => void
}

export function RatingControl({ value, onChange }: RatingControlProps) {
  const [focused, setFocused] = useState(0)
  const current = focused || value
  return (
    <fieldset className="rating-control">
      <legend>Your rating on this device</legend>
      <div className="rating-control__stars" onMouseLeave={() => setFocused(0)}>
        {[1, 2, 3, 4, 5].map((star) => <button key={star} type="button" className={star <= current ? 'is-active' : ''} aria-label={`${star} out of 5`} aria-pressed={value === star} onFocus={() => setFocused(star)} onMouseEnter={() => setFocused(star)} onClick={() => onChange(star)}>★</button>)}
      </div>
      <span className="rating-control__note">Saved locally in this browser</span>
    </fieldset>
  )
}
