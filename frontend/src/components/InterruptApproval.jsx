// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState } from 'react'

export default function InterruptApproval({ interrupt, onApprove, onReject, responseType: parentResponseType }) {
  const [isProcessing, setIsProcessing] = useState(false)
  const [localResponseType, setLocalResponseType] = useState(null)
  const responseType = parentResponseType || localResponseType

  const handleApprove = async () => {
    if (responseType) return
    setIsProcessing(true)
    setLocalResponseType('approved')
    await onApprove(interrupt.interrupt_id)
  }

  const handleReject = async () => {
    if (responseType) return
    setIsProcessing(true)
    setLocalResponseType('rejected')
    await onReject(interrupt.interrupt_id)
  }

  const message = interrupt.reason?.message || 'Approval required'

  return (
    <div className="my-3 bg-warn-bg border-2 border-warn-border rounded-lg p-4 shadow-sm">
      <div className="flex items-start gap-4">
        <div className="text-2xl">⚠️</div>
        <div className="flex-1">
          <p className="text-warn-text font-medium mb-3">{message}</p>
          {responseType ? (
            <div className={`px-4 py-2.5 rounded-lg font-medium text-sm inline-flex items-center gap-2 ${
              responseType === 'approved'
                ? 'bg-ok-bg-2 text-ok-text border border-ok-border'
                : 'bg-err-bg-2 text-err-text border border-err-border'
            }`}>
              <span>{responseType === 'approved' ? '✓ Approved' : '✕ Rejected'}</span>
            </div>
          ) : (
            <div className="flex gap-3">
              <button onClick={handleApprove} disabled={isProcessing}
                className="px-6 py-2.5 rounded-lg font-medium text-sm bg-ok text-white hover:bg-ok disabled:bg-fg-muted transition-colors">
                {isProcessing ? 'Processing...' : 'Approve'}
              </button>
              <button onClick={handleReject} disabled={isProcessing}
                className="px-6 py-2.5 rounded-lg font-medium text-sm bg-err text-white hover:bg-err disabled:bg-fg-muted transition-colors">
                {isProcessing ? 'Processing...' : 'Reject'}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
