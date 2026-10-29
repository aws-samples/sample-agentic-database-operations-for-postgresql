// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useRef } from 'react'
import InterruptApproval from './InterruptApproval'
import InvestigationSuggestion from './InvestigationSuggestion'
import InvestigationHandoff from './InvestigationHandoff'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { APP_NAME } from '../appName'

function CopyIcon({ className }) {
  return <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" /></svg>
}
function CheckIcon({ className }) {
  return <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" /></svg>
}
function DownloadIcon({ className }) {
  return <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" /></svg>
}

function tableToCSV(tableEl) {
  const rows = []
  tableEl.querySelectorAll('tr').forEach(tr => {
    const cells = []
    tr.querySelectorAll('th, td').forEach(td => {
      let val = td.textContent.replace(/"/g, '""')
      if (val.includes(',') || val.includes('\n')) val = `"${val}"`
      cells.push(val)
    })
    rows.push(cells.join(','))
  })
  return rows.join('\n')
}

function TableWrapper({ children }) {
  const ref = useRef(null)
  const [copied, setCopied] = useState(false)

  const handleCopy = () => {
    if (!ref.current) return
    navigator.clipboard.writeText(tableToCSV(ref.current))
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const handleDownload = () => {
    if (!ref.current) return
    const csv = tableToCSV(ref.current)
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `table-${new Date().toISOString().slice(0, 19)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="relative group/table my-2">
      <div className="absolute -top-1 right-0 flex items-center gap-0.5 opacity-0 group-hover/table:opacity-100 transition-opacity z-10">
        <button onClick={handleCopy} title="Copy as CSV"
          className="p-1 bg-surface border border-line rounded shadow-sm text-fg-muted hover:text-primary transition-colors">
          {copied ? <CheckIcon className="w-3 h-3 text-ok" /> : <CopyIcon className="w-3 h-3" />}
        </button>
        <button onClick={() => handleDownload()} title="Download CSV"
          className="p-1 bg-surface border border-line rounded shadow-sm text-fg-muted hover:text-primary transition-colors">
          <DownloadIcon className="w-3 h-3" />
        </button>
      </div>
      <table ref={ref}>{children}</table>
    </div>
  )
}

function MessageActions({ content }) {
  const [copied, setCopied] = useState(false)
  const handleCopy = () => { navigator.clipboard.writeText(content); setCopied(true); setTimeout(() => setCopied(false), 2000) }
  const handleDownload = () => {
    const blob = new Blob([content], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href = url; a.download = `agent-response-${new Date().toISOString().slice(0, 19)}.txt`; a.click()
    URL.revokeObjectURL(url)
  }
  if (!content) return null
  return (
    <div className="flex items-center gap-1 mt-1.5 opacity-0 group-hover:opacity-100 transition-opacity">
      <button onClick={handleCopy} title="Copy" className="p-1 text-fg-muted hover:text-primary rounded transition-colors">
        {copied ? <CheckIcon className="w-3.5 h-3.5 text-ok" /> : <CopyIcon className="w-3.5 h-3.5" />}
      </button>
      <button onClick={handleDownload} title="Download" className="p-1 text-fg-muted hover:text-primary rounded transition-colors">
        <DownloadIcon className="w-3.5 h-3.5" />
      </button>
    </div>
  )
}

const mdComponents = {
  table: ({ children }) => <TableWrapper>{children}</TableWrapper>,
}

export default function ChatMessage({ message, isStreaming, onApprove, onReject, respondedInterrupts, userName, onStartInvestigation, onOpenInvestigation, onHandoffPick, handoffDisabled, linkedInvestigations, databases }) {
  const isUser = message.role === 'user'
  const plainText = message.contentBlocks?.filter(b => b.type === 'text').map(b => b.content).join('\n') || message.content || ''
  const persona = isUser ? (userName || 'You') : APP_NAME

  return (
    <div className={`flex w-full ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div className={`flex flex-col max-w-[85%] ${isUser ? 'items-end' : 'items-start'}`}>
        {/* Persona label — makes it clear who is speaking (user vs the agent). */}
        <div className={`flex items-center gap-1 mb-1 px-1 text-[11px] font-medium text-fg-muted ${isUser ? 'flex-row-reverse' : ''}`}>
          {!isUser && (
            <svg className="w-3 h-3 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
            </svg>
          )}
          <span className="truncate max-w-[220px]">{persona}</span>
        </div>
        {/* User turns sit in a neutral bubble (no colour fill, no shadow);
            the agent's reply keeps a light card so tool chips have a frame. */}
        <div className={`group rounded-2xl px-4 py-2.5 ${
          isUser ? 'bg-bubble text-bubble-fg rounded-br-none' : 'bg-surface text-fg rounded-bl-none border border-line shadow-sm'
        }`}>
          <div className="text-sm leading-relaxed">
            {isUser ? (
              <div className="whitespace-pre-wrap break-words">{message.content}</div>
            ) : message.contentBlocks?.length > 0 ? (
              <div className="space-y-2">
                {message.contentBlocks.map((block, index) => {
                  if (block.type === 'text') {
                    return (
                      <div key={`text-${index}`} className="prose prose-sm max-w-none prose-p:my-1">
                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>{block.content}</ReactMarkdown>
                        {isStreaming && index === message.contentBlocks.length - 1 && (
                          <span className="inline-block ml-0.5 text-primary animate-cursor-blink">▋</span>
                        )}
                      </div>
                    )
                  } else if (block.type === 'thinking') {
                    const progress = block.completed ? `${block.completed}/${block.count}` : ''
                    return (
                      <div key={`thinking-${block.toolUseId}`} className="flex items-center gap-2 py-2 px-3 my-2 bg-surface-2 rounded-lg border border-line">
                        <div className="flex gap-1">
                          <div className="w-1.5 h-1.5 rounded-full bg-primary animate-bounce" style={{ animationDelay: '0ms' }} />
                          <div className="w-1.5 h-1.5 rounded-full bg-primary animate-bounce" style={{ animationDelay: '150ms' }} />
                          <div className="w-1.5 h-1.5 rounded-full bg-primary animate-bounce" style={{ animationDelay: '300ms' }} />
                        </div>
                        <span className="text-xs text-fg-3">
                          {block.agent}
                          {block.count > 1 ? ` · checking ${block.count} databases` : ''}
                          {block.currentTool ? ` · ${block.currentTool}` : ''}
                          {progress ? ` (${progress})` : '...'}
                        </span>
                      </div>
                    )
                  } else if (block.type === 'tool_mini') {
                    if (block.isSkill) {
                      return (
                        <div key={`skill-${block.toolUseId}`} className="inline-flex items-center gap-1.5 px-2.5 py-1 my-1 bg-primary-subtle rounded-full border border-primary-line text-[11px] text-primary">
                          <span aria-hidden>🧠</span>
                          <span className="font-medium">Skill: {block.skillName || 'loaded'}</span>
                        </div>
                      )
                    }
                    return (
                      <div key={`mini-${block.toolUseId}`} className="inline-flex items-center gap-1.5 px-2.5 py-1 my-1 bg-surface-2 rounded-full border border-line text-[11px] text-fg-3">
                        <svg className="w-3 h-3 text-fg-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
                        </svg>
                        {block.name}
                      </div>
                    )
                  } else if (block.type === 'interrupt') {
                    return (
                      <InterruptApproval key={`int-${block.interrupt.interrupt_id}`} interrupt={block.interrupt}
                        onApprove={onApprove} onReject={onReject} responseType={respondedInterrupts?.get(block.interrupt.interrupt_id)} />
                    )
                  } else if (block.type === 'investigation_suggestion') {
                    return (
                      <InvestigationSuggestion key={`inv-sug-${block.toolUseId}`} suggestion={block}
                        onStart={onStartInvestigation} onOpen={onOpenInvestigation}
                        databases={databases}
                        linkedInvestigations={linkedInvestigations} />
                    )
                  } else if (block.type === 'investigation_handoff') {
                    return (
                      <InvestigationHandoff key={`inv-handoff-${block.investigation?.investigation_id}`} investigation={block.investigation}
                        onOpen={onOpenInvestigation} onPick={onHandoffPick} disabled={handoffDisabled} />
                    )
                  }
                  return null
                })}
              </div>
            ) : (
              <div className="prose prose-sm max-w-none prose-p:my-1">
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>{message.content || ''}</ReactMarkdown>
              </div>
            )}
          </div>
          <div className="flex items-center justify-between">
            <div className="text-[10px] mt-1.5 text-fg-muted">
              {new Date(message.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
            </div>
            {!isUser && !isStreaming && <MessageActions content={plainText} />}
          </div>
        </div>
      </div>
    </div>
  )
}
