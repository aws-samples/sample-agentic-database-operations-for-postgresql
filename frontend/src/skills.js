// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Skill-activation detection for the agent's built-in Agent Skills
// (Strands AgentSkills / agentskills.io). Progressive disclosure means the
// agent "loads" a skill by calling a plugin-registered tool whose argument
// names the skill. The exact tool name and argument key can vary by Strands
// version, so we match defensively:
//   - name: "skills" / "skill" / anything containing "skill"
//   - skill name: common arg keys, else the first string-valued arg
// No first-party tool in this app is named with "skill", so the substring
// guard is safe here (it won't misfire on get_vacuum_stats_health, etc.).

export function isSkillActivation(toolName) {
  if (!toolName) return false
  return String(toolName).toLowerCase().includes('skill')
}

export function skillNameFromInput(input) {
  if (!input || typeof input !== 'object') return ''
  const keyed =
    input.skill_name || input.skill || input.name || input.skill_id || input.id
  if (keyed) return String(keyed)
  // Fallback: first non-empty string-valued field.
  const firstStr = Object.values(input).find(
    (v) => typeof v === 'string' && v.trim()
  )
  return firstStr ? String(firstStr) : ''
}
