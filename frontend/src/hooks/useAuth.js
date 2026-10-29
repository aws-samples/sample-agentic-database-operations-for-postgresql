// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useState, useEffect, useRef } from 'react'
import { jwtDecode } from 'jwt-decode'
import { config } from '../config'

const COGNITO_DOMAIN = config.cognitoDomain
const CLIENT_ID = config.clientId
const AWS_REGION = config.awsRegion
const COGNITO_URL = `https://cognito-idp.${AWS_REGION}.amazonaws.com`

export default function useAuth() {
  const [token, setToken] = useState(null)
  const [accessToken, setAccessToken] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [needsLogin, setNeedsLogin] = useState(false)

  const isTokenExpired = (token) => {
    try {
      const decoded = jwtDecode(token)
      return decoded.exp * 1000 < Date.now() + 30000
    } catch { return true }
  }

  const refreshToken = async () => {
    try {
      const storedRefreshToken = localStorage.getItem('refresh_token')
      if (!storedRefreshToken) throw new Error('No refresh token')

      const response = await fetch(
        `https://${COGNITO_DOMAIN}.auth.${AWS_REGION}.amazoncognito.com/oauth2/token`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: new URLSearchParams({
            grant_type: 'refresh_token', client_id: CLIENT_ID, refresh_token: storedRefreshToken
          })
        }
      )
      if (!response.ok) throw new Error('Token refresh failed')

      const data = await response.json()
      localStorage.setItem('access_token', data.access_token)
      localStorage.setItem('id_token', data.id_token)
      if (data.refresh_token) localStorage.setItem('refresh_token', data.refresh_token)

      setToken(data.id_token)
      setAccessToken(data.access_token)
      return { idToken: data.id_token, accessToken: data.access_token }
    } catch (err) {
      localStorage.removeItem('access_token')
      localStorage.removeItem('refresh_token')
      localStorage.removeItem('id_token')
      setNeedsLogin(true)
      throw err
    }
  }

  // useRef so the in-flight refresh is shared across renders — a plain `let` resets
  // every render, which lets concurrent callers each start their own refresh.
  const _refreshPromise = useRef(null)

  const getValidAccessToken = async () => {
    const storedToken = localStorage.getItem('access_token')
    if (!storedToken) { setNeedsLogin(true); return null }
    if (!isTokenExpired(storedToken)) return storedToken

    if (!_refreshPromise.current) {
      _refreshPromise.current = refreshToken().finally(() => { _refreshPromise.current = null })
    }
    const tokens = await _refreshPromise.current
    return tokens.accessToken
  }

  // A refresh regardless of expiry (the server refused the current token), sharing the
  // in-flight one. On failure refreshToken has already cleared the session.
  const forceRefresh = async () => {
    if (!_refreshPromise.current) {
      _refreshPromise.current = refreshToken().finally(() => { _refreshPromise.current = null })
    }
    try {
      return await _refreshPromise.current
    } catch (e) {
      // Signed out server-side (role changed, account deleted): show the sign-in page
      // rather than a signed-in shell whose every request fails.
      setToken(null)
      setAccessToken(null)
      throw e
    }
  }
  const getValidIdToken = async () => {
    const storedToken = localStorage.getItem('id_token')
    if (!storedToken) { setNeedsLogin(true); return null }
    if (!isTokenExpired(storedToken)) return storedToken

    if (!_refreshPromise.current) {
      _refreshPromise.current = refreshToken().finally(() => { _refreshPromise.current = null })
    }
    const tokens = await _refreshPromise.current
    return tokens.idToken
  }

  // Force change password + TOTP MFA challenge state
  const [challengeSession, setChallengeSession] = useState(null)
  const [challengeEmail, setChallengeEmail] = useState(null)
  // null | 'request' (ask for the emailed code) | 'confirm' (code + new password).
  const [forgotMode, setForgotMode] = useState(null)
  const [mfaMode, setMfaMode] = useState(null)          // null | 'setup' | 'challenge'
  const [mfaSecret, setMfaSecret] = useState(null)      // base32 TOTP secret (enrollment)
  const [mfaOtpauthUri, setMfaOtpauthUri] = useState(null)
  const MFA_ISSUER = 'DBOps Agent'

  const cognitoCall = async (target, body) => {
    const response = await fetch(COGNITO_URL, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-amz-json-1.1',
        'X-Amz-Target': `AWSCognitoIdentityProviderService.${target}`,
      },
      body: JSON.stringify(body),
    })
    const data = await response.json().catch(() => ({}))
    return { ok: response.ok, data }
  }

  // Drop any in-progress challenge and show the sign-in form.
  const backToSignIn = () => {
    setChallengeSession(null)
    setMfaMode(null); setMfaSecret(null); setMfaOtpauthUri(null)
    setForgotMode(null)
    setNeedsLogin(true)
  }

  // The challenge session (NEW_PASSWORD / MFA setup) is short-lived — AuthSessionValidity
  // on the web client, raised to the 15-minute max in cognito.yaml. When it lapses mid
  // setup Cognito returns NotAuthorizedException with an "expired"/"session" message;
  // say so plainly and send the user back to sign in rather than showing a raw error.
  const sessionExpiredMessage = (data) => {
    const t = `${data?.__type || ''} ${data?.message || ''}`.toLowerCase()
    if (t.includes('expired') || (t.includes('session') && t.includes('invalid'))) {
      backToSignIn()
      return 'Your session expired. Sign in again with your new password.'
    }
    return ''
  }

  // Persist tokens and clear all challenge state.
  const finalizeAuth = (result) => {
    const { IdToken, AccessToken, RefreshToken } = result
    localStorage.setItem('id_token', IdToken)
    localStorage.setItem('access_token', AccessToken)
    localStorage.setItem('refresh_token', RefreshToken)
    setToken(IdToken)
    setAccessToken(AccessToken)
    setChallengeSession(null); setChallengeEmail(null)
    setForgotMode(null)
    setMfaMode(null); setMfaSecret(null); setMfaOtpauthUri(null)
    setNeedsLogin(false); setLoading(false)
    return true
  }

  // Route an InitiateAuth / RespondToAuthChallenge response to the next step.
  const routeChallenge = async (data, email) => {
    switch (data.ChallengeName) {
      case 'NEW_PASSWORD_REQUIRED':
        setChallengeSession(data.Session); setChallengeEmail(email)
        return 'NEW_PASSWORD_REQUIRED'
      case 'SOFTWARE_TOKEN_MFA':
        setChallengeSession(data.Session); setChallengeEmail(email); setMfaMode('challenge')
        return 'SOFTWARE_TOKEN_MFA'
      case 'MFA_SETUP': {
        const { ok, data: assoc } = await cognitoCall('AssociateSoftwareToken', { Session: data.Session })
        if (!ok || !assoc.SecretCode) {
          setError(sessionExpiredMessage(assoc) || assoc.message || assoc.__type || 'Could not start MFA setup')
          return false
        }
        setChallengeSession(assoc.Session); setChallengeEmail(email)
        setMfaSecret(assoc.SecretCode)
        setMfaOtpauthUri(`otpauth://totp/${encodeURIComponent(MFA_ISSUER)}:${encodeURIComponent(email)}?secret=${assoc.SecretCode}&issuer=${encodeURIComponent(MFA_ISSUER)}`)
        setMfaMode('setup')
        return 'MFA_SETUP'
      }
      default:
        if (data.AuthenticationResult) return finalizeAuth(data.AuthenticationResult)
        setError(data.message || data.__type || 'Login failed')
        return false
    }
  }

  // Direct email/password login via Cognito InitiateAuth
  const login = async (email, password) => {
    setError(null)
    try {
      const { data } = await cognitoCall('InitiateAuth', {
        AuthFlow: 'USER_PASSWORD_AUTH',
        ClientId: CLIENT_ID,
        AuthParameters: { USERNAME: email, PASSWORD: password },
      })
      // An admin reset (console "Reset password") leaves the user in RESET_REQUIRED;
      // Cognito refuses sign-in until a new password is set with the emailed code. Send
      // the operator to the confirm-code screen rather than a dead-end error.
      if (data.__type && data.__type.includes('PasswordResetRequiredException')) {
        setChallengeEmail(email); setForgotMode('confirm')
        setError('Your password was reset. Enter the code we emailed you and choose a new password.')
        return 'RESET_REQUIRED'
      }
      return await routeChallenge(data, email)
    } catch (err) {
      setError(err.message || 'Login failed')
      return false
    }
  }

  // Self-service reset. ForgotPassword emails a code; ConfirmForgotPassword redeems it
  // with a new password. Both are unauthenticated by design (the user has no session),
  // and Cognito throttles ForgotPassword server-side. PreventUserExistenceErrors on the
  // client keeps the "code sent" response identical for a known and an unknown address,
  // so this screen never reveals whether an account exists.
  const forgotPassword = async (email) => {
    setError(null)
    try {
      const { ok, data } = await cognitoCall('ForgotPassword', { ClientId: CLIENT_ID, Username: email })
      if (!ok) { setError(data.message || data.__type || 'Could not send a reset code'); return false }
      setChallengeEmail(email); setForgotMode('confirm')
      return true
    } catch (err) {
      setError(err.message || 'Could not send a reset code')
      return false
    }
  }

  const confirmForgotPassword = async (code, newPassword) => {
    setError(null)
    try {
      const { ok, data } = await cognitoCall('ConfirmForgotPassword', {
        ClientId: CLIENT_ID, Username: challengeEmail, ConfirmationCode: code, Password: newPassword,
      })
      if (!ok) { setError(data.message || data.__type || 'Could not reset the password'); return false }
      // Reset done, but no tokens are issued here: send the user back to sign in with
      // the new password (which then runs the normal MFA step).
      setForgotMode(null)
      setError('Password changed. Sign in with your new password.')
      return true
    } catch (err) {
      setError(err.message || 'Could not reset the password')
      return false
    }
  }

  const startForgotPassword = () => { setError(null); setForgotMode('request') }
  const cancelForgotPassword = () => { setError(null); setForgotMode(null) }

  // Complete the NEW_PASSWORD_REQUIRED challenge (may lead to MFA_SETUP when MFA is required).
  const completeNewPassword = async (newPassword) => {
    setError(null)
    try {
      const { data } = await cognitoCall('RespondToAuthChallenge', {
        ChallengeName: 'NEW_PASSWORD_REQUIRED',
        ClientId: CLIENT_ID,
        Session: challengeSession,
        ChallengeResponses: { USERNAME: challengeEmail, NEW_PASSWORD: newPassword },
      })
      return await routeChallenge(data, challengeEmail)
    } catch (err) {
      setError(err.message || 'Password change failed')
      return false
    }
  }

  // TOTP enrollment: verify the first code and complete the MFA_SETUP challenge.
  const completeMfaSetup = async (code) => {
    setError(null)
    try {
      const v = await cognitoCall('VerifySoftwareToken', {
        Session: challengeSession, UserCode: code, FriendlyDeviceName: 'authenticator',
      })
      if (!v.ok || v.data.Status !== 'SUCCESS') {
        const expired = sessionExpiredMessage(v.data)
        if (expired) { setError(expired); return false }
        if (v.data.Session) setChallengeSession(v.data.Session)
        setError(v.data.message || v.data.__type || 'Invalid code — try again')
        return false
      }
      const { data } = await cognitoCall('RespondToAuthChallenge', {
        ChallengeName: 'MFA_SETUP', ClientId: CLIENT_ID, Session: v.data.Session,
        ChallengeResponses: { USERNAME: challengeEmail },
      })
      return await routeChallenge(data, challengeEmail)
    } catch (err) {
      setError(err.message || 'MFA setup failed')
      return false
    }
  }

  // TOTP login challenge.
  const submitMfaCode = async (code) => {
    setError(null)
    try {
      const { ok, data } = await cognitoCall('RespondToAuthChallenge', {
        ChallengeName: 'SOFTWARE_TOKEN_MFA', ClientId: CLIENT_ID, Session: challengeSession,
        ChallengeResponses: { USERNAME: challengeEmail, SOFTWARE_TOKEN_MFA_CODE: code },
      })
      if (!ok && data.Session) setChallengeSession(data.Session)
      return await routeChallenge(data, challengeEmail)
    } catch (err) {
      setError(err.message || 'MFA verification failed')
      return false
    }
  }

  useEffect(() => { initAuth() }, [])

  const initAuth = async () => {
    try {
      const storedIdToken = localStorage.getItem('id_token')
      const storedAccessToken = localStorage.getItem('access_token')

      if (storedIdToken && storedAccessToken) {
        if (isTokenExpired(storedIdToken)) {
          try { await refreshToken(); setLoading(false); return }
          catch { setNeedsLogin(true); setLoading(false); return }
        }
        setToken(storedIdToken)
        setAccessToken(storedAccessToken)
        setLoading(false)
        return
      }
      setNeedsLogin(true)
      setLoading(false)
    } catch (err) {
      setError(err.message)
      setNeedsLogin(true)
      setLoading(false)
    }
  }

  // Kill the refresh token server-side so a copy (e.g. lifted from localStorage via XSS)
  // cannot be exchanged for new tokens after logout. Best-effort and keepalive so it is not
  // cancelled by the navigation that usually follows logout; requires EnableTokenRevocation
  // on the Cognito client (set in cognito.yaml). The local clear below always runs regardless.
  const revokeRefreshToken = () => {
    try {
      const rt = localStorage.getItem('refresh_token')
      if (!rt) return
      fetch(`https://${COGNITO_DOMAIN}.auth.${AWS_REGION}.amazoncognito.com/oauth2/revoke`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ token: rt, client_id: CLIENT_ID }),
        keepalive: true,
      }).catch(() => {})
    } catch { /* best-effort: never block logout on a revoke failure */ }
  }

  const logout = () => {
    revokeRefreshToken()
    localStorage.removeItem('access_token')
    localStorage.removeItem('refresh_token')
    localStorage.removeItem('id_token')
    setToken(null)
    setAccessToken(null)
    setNeedsLogin(true)
  }

  // ── Self-service profile (Cognito user-context calls via the access token) ──
  const updateName = async (name) => {
    try {
      const at = await getValidAccessToken()
      if (!at) return { ok: false, error: 'Not signed in' }
      const res = await fetch(COGNITO_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-amz-json-1.1',
          'X-Amz-Target': 'AWSCognitoIdentityProviderService.UpdateUserAttributes',
        },
        body: JSON.stringify({ AccessToken: at, UserAttributes: [{ Name: 'name', Value: name }] }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        return { ok: false, error: data.message || data.__type || 'Update failed' }
      }
      // Refresh so the new name lands in the id token (greeting + profile).
      try { await refreshToken() } catch {}
      return { ok: true }
    } catch (err) {
      return { ok: false, error: err.message || 'Update failed' }
    }
  }

  const changePassword = async (oldPassword, newPassword) => {
    try {
      const at = await getValidAccessToken()
      if (!at) return { ok: false, error: 'Not signed in' }
      const res = await fetch(COGNITO_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-amz-json-1.1',
          'X-Amz-Target': 'AWSCognitoIdentityProviderService.ChangePassword',
        },
        body: JSON.stringify({ AccessToken: at, PreviousPassword: oldPassword, ProposedPassword: newPassword }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        return { ok: false, error: data.message || data.__type || 'Password change failed' }
      }
      return { ok: true }
    } catch (err) {
      return { ok: false, error: err.message || 'Password change failed' }
    }
  }

  return {
    token, accessToken, loading, error, logout, login, needsLogin,
    isAuthenticated: !!token, getValidAccessToken, getValidIdToken,
    // Exposed so the app can force a refresh: Cognito bakes group membership into a
    // token at login, so a space granted mid-session only shows up after a new token.
    refreshToken, forceRefresh,
    challengeSession, completeNewPassword, updateName, changePassword,
    mfaMode, mfaSecret, mfaOtpauthUri, completeMfaSetup, submitMfaCode,
    forgotMode, startForgotPassword, cancelForgotPassword, forgotPassword, confirmForgotPassword,
  }
}
