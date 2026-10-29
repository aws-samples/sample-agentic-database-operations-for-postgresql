// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Small pill toggle switch, theme-aware. Used for enable/disable
// of inherited knowledge and to show built-ins as locked/always-on.
export default function Toggle({ checked, disabled = false, onChange, title, size = 'md' }) {
  const dims = size === 'sm'
    ? { track: 'h-4 w-7', knob: 'h-3 w-3', on: 'translate-x-3.5', off: 'translate-x-0.5' }
    : { track: 'h-5 w-9', knob: 'h-4 w-4', on: 'translate-x-4', off: 'translate-x-0.5' }
  return (
    <button
      type="button" role="switch" aria-checked={checked} disabled={disabled} title={title}
      onClick={(e) => { e.stopPropagation(); if (!disabled) onChange?.(!checked) }}
      className={`relative inline-flex ${dims.track} flex-shrink-0 items-center rounded-full transition-colors
        ${checked ? 'bg-primary' : 'bg-line-strong'}
        ${disabled ? 'opacity-70 cursor-not-allowed' : 'cursor-pointer'}`}
    >
      <span className={`inline-block ${dims.knob} transform rounded-full bg-surface shadow-sm transition-transform
        ${checked ? dims.on : dims.off}`} />
    </button>
  )
}
