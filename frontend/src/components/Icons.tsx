interface IconProps {
  label?: string
  size?: number
}

export function Icon({ label, size = 18 }: IconProps) {
  return <span className="icon" style={{ fontSize: size }} aria-hidden={label ? undefined : true}>{label ?? '•'}</span>
}

export function ArrowIcon() { return <Icon label="↗" size={20} /> }
export function SearchIcon() { return <Icon label="⌕" size={23} /> }
export function BookIcon() { return <Icon label="▤" size={19} /> }
export function SparkIcon() { return <Icon label="✦" size={18} /> }
export function UserIcon() { return <Icon label="◌" size={21} /> }
