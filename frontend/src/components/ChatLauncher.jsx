// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// The floating sparkle button that re-opens the chat when it is collapsed
// behind a workspace panel. `unread` marks that a
// reply finished while the chat was hidden.
export default function ChatLauncher({ onClick, unread }) {
  return (
    <button type="button" onClick={onClick} className="chat-launcher"
      title={unread ? 'Open chat — new reply' : 'Open chat'} aria-label={unread ? 'Open chat, new reply' : 'Open chat'}>
      <svg className="w-6 h-6" viewBox="0 0 24 24" fill="currentColor" aria-hidden>
        <path d="M12 2.5c.35 3.9 1.6 6.6 3.7 8.1 1.4 1 3.2 1.4 5.8 1.4-3.9.35-6.6 1.6-8.1 3.7-1 1.4-1.4 3.2-1.4 5.8-.35-3.9-1.6-6.6-3.7-8.1-1.4-1-3.2-1.4-5.8-1.4 3.9-.35 6.6-1.6 8.1-3.7 1-1.4 1.4-3.2 1.4-5.8z" />
      </svg>
      {unread && <span className="chat-launcher-dot" aria-hidden />}
    </button>
  )
}
