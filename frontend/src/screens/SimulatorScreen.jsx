import { useEffect, useState, useRef } from 'react'

import { API_URL, WS_BRIDGE_URL } from '../config'

function countdownDisplay(state) {
  if (state?.phase !== 'countdown') return null
  const s = state.countdown_step
  if (s === 'go' || s === 0) return 'GO!'
  return String(s ?? '')
}

function isInputBlocked(state) {
  if (!state) return true
  if (state.accepting_input === false) return true
  return state.phase && state.phase !== 'playing'
}

function showPhaseOverlay(state) {
  return ['countdown', 'level_clear', 'level_fail'].includes(state?.phase)
}

/** 2P when settings, login, or DK level say so (cardId2 is the strongest signal). */
function effectivePlayerCount(config) {
  if (config.playMode === 'group') return 1
  if (config.cardId2) return 2
  if ((config.playerCount || 1) >= 2) return 2
  if (String(config.level || '').toUpperCase().startsWith('DK')) return 2
  return config.playerCount || 1
}

function HeartRow({ life, maxLife }) {
  // Cap visual hearts (backend display_max is typically 5)
  const total = Math.max(1, Math.min(10, Math.round(maxLife) || 5))
  const filled = Math.max(0, Math.min(total, Math.round(life)))
  return (
    <div className="hud-hearts" aria-label={`${filled} of ${total} lives`}>
      {Array.from({ length: total }, (_, i) => (
        <span key={i} className={`hud-heart ${i < filled ? 'filled' : 'empty'}`}>♥</span>
      ))}
    </div>
  )
}

export default function SimulatorScreen({ config, onGameEnd }) {
  const [gameState, setGameState] = useState(null)
  const [gameId, setGameId] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [stopping, setStopping] = useState(false)
  const [showSim, setShowSim] = useState(false)
  const iframeRef = useRef(null)
  const gameIdRef = useRef(null)
  const endedRef = useRef(false)
  const stateRef = useRef(null)
  // Audio: synth beeps via Web Audio (no asset files needed)
  const audioCtxRef = useRef(null)
  const prevScoreRef = useRef(0)
  const prevScore2Ref = useRef(0)
  const prevLifeRef = useRef(null)
  const startedRef = useRef(false)

  const beep = (freq, durMs, type = 'sine', gain = 0.15) => {
    try {
      if (!audioCtxRef.current) {
        audioCtxRef.current = new (window.AudioContext || window.webkitAudioContext)()
      }
      const ctx = audioCtxRef.current
      const osc = ctx.createOscillator()
      const g = ctx.createGain()
      osc.type = type
      osc.frequency.value = freq
      g.gain.value = gain
      osc.connect(g); g.connect(ctx.destination)
      osc.start()
      g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + durMs / 1000)
      osc.stop(ctx.currentTime + durMs / 1000)
    } catch (e) { /* audio not available */ }
  }
  const playScore = () => beep(880, 120, 'triangle', 0.18)   // bright ding
  const playHurt = () => beep(140, 220, 'sawtooth', 0.22)    // low buzz

  // Start game on mount (or resume an already-running game after reload)
  useEffect(() => {
    if (startedRef.current) return
    startedRef.current = true
    const startGame = async () => {
      try {
        const response = await fetch(`${API_URL}/start-game`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            card_id: config.cardId,
            level: config.playMode === 'group' ? (config.level || 'auto') : (config.level || '001'),
            difficulty: config.difficulty || 'normal',
            player_count: config.playMode === 'group' ? 1 : effectivePlayerCount(config),
            ...(config.playMode === 'group' ? { mode: 'group' } : {}),
          })
        })
        const data = await response.json()
        if (data.success) {
          setGameId(data.game_id)
          gameIdRef.current = data.game_id
          setLoading(false)
        } else {
          setError(data.error || 'Failed to start game')
        }
      } catch (err) {
        setError(err.message)
      }
    }

    const resumeOrStart = async () => {
      if (!config.resumeGameId) {
        await startGame()
        return
      }
      // Resume only if backend already has 2P scoring when this session expects it.
      try {
        const want2P = effectivePlayerCount(config) >= 2
        const res = await fetch(`${API_URL}/game-state/${config.resumeGameId}`)
        const data = await res.json()
        if (data.success && (!want2P || data.state?.multiplayer)) {
          setGameId(config.resumeGameId)
          gameIdRef.current = config.resumeGameId
          setLoading(false)
          return
        }
      } catch (err) {
        console.warn('Resume check failed, starting fresh:', err)
      }
      await startGame()
    }

    resumeOrStart()
  }, [config])

  // End the game: stop on backend, record, route to result panel
  const endGame = async (reason) => {
    if (endedRef.current) return
    endedRef.current = true
    setStopping(true)
    const id = gameIdRef.current
    const st = stateRef.current || {}
    const finalScore = st.score || 0
    const finalScore2 = st.score2 || 0
    const finalMultiplayer = st.multiplayer || effectivePlayerCount(config) >= 2
    const finalTime = st.time_elapsed || 0
    const finalLife = st.life ?? 0
    // out_of_life beats the passed reason (game ended because HP hit 0)
    const finalReason = st.game_over_reason === 'out_of_life'
      ? 'out_of_life' : (reason || 'stopped')
    try {
      // Persist score to leaderboard
      await fetch(`${API_URL}/save-score`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          card_id: config.cardId,
          card_id2: config.cardId2 || null,
          level: config.level,                       // starting level picked
          end_level: st.current_level ?? config.level, // level ended on
          score: finalScore,                         // raw P1 (on-screen)
          score2: finalScore2,                       // raw P2 (on-screen)
          final_score: st.final_score ?? finalScore,   // normalized P1
          final_score2: st.final_score2 ?? finalScore2,// normalized P2
          multiplayer: finalMultiplayer,
          life: finalLife,
          lives_start: st.max_life ?? 0,
          result: st.result ?? null,
          time_used: finalTime,                      // full session duration
          levels_cleared: st.levels_cleared ?? 0,
          difficulty: config.difficulty ?? '',
          started_at: st.started_at ?? ''
        })
      })
      await fetch(`${API_URL}/logout`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ card_id: config.cardId, game_id: id })
      })
    } catch (err) {
      console.error('Stop/save error:', err)
    }
    onGameEnd({
      score: finalScore,
      score2: finalScore2,
      multiplayer: finalMultiplayer,
      time_elapsed: finalTime,
      life: finalLife,
      level: config.level,
      game: config.game,
      difficulty: config.difficulty,
      reason: finalReason
    })
  }

  // Poll game state; auto-end on timeout/game_over
  useEffect(() => {
    if (!gameId) return
    const pollState = async () => {
      try {
        // Poll THIS game specifically (avoid stale "first active" game)
        const response = await fetch(`${API_URL}/game-state/${gameId}`)
        const data = await response.json()
        if (data.success) {
          const st = data.state
          const backendAudio = st.backend_audio === true
          const phase = st.phase || 'idle'
          const inputLive = phase === 'playing' && st.accepting_input !== false
          // Backend AudioManager is authoritative — mute FE synth when it is active.
          if (!backendAudio && inputLive) {
            if (st.score > prevScoreRef.current || st.score2 > prevScore2Ref.current) playScore()
            if (prevLifeRef.current !== null && st.life < prevLifeRef.current) playHurt()
          }
          prevScoreRef.current = st.score
          prevScore2Ref.current = st.score2 || 0
          prevLifeRef.current = st.life

          setGameState(st)
          stateRef.current = st
          if (st.game_over && !endedRef.current) {
            endGame('timeout')
          }
        }
      } catch (err) {
        console.error('Poll error:', err)
      }
    }
    const interval = setInterval(pollState, 100)
    return () => clearInterval(interval)
  }, [gameId])

  // Background music: looped track during gameplay, low under SFX.
  // Browser-safe: starts after user clicks (game start), stops on exit.
  useEffect(() => {
    if (!gameId) return
    const bgm = new Audio('/media/bgm.mp3')
    bgm.loop = true
    bgm.volume = 0.25
    bgm.play().catch(() => {})
    return () => { bgm.pause() }
  }, [gameId])

  if (loading) {
    return (
      <div className="screen">
        <div className="card">
          <h2>Starting Game...</h2>
          <p style={{ textAlign: 'center', marginTop: '20px' }}>
            {(config.game || 'hoops').toUpperCase()} - Level {config.level} ({config.difficulty})
          </p>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="screen">
        <div className="card">
          <h2>Error</h2>
          <p style={{ color: 'var(--color-error)', marginTop: '20px' }}>{error}</p>
        </div>
      </div>
    )
  }

  const timeLeft = gameState?.time_left != null ? gameState.time_left : 300
  // Hearts: 5 shown (each absorbs a share of mistakes scaled to this game's own
  // max_life). Backend sends display_lives/display_max; fall back to raw HP.
  const life = gameState?.display_lives ?? gameState?.life ?? gameState?.max_life ?? 0
  const maxLife = gameState?.display_max ?? gameState?.max_life ?? 5
  const isOver = gameState?.game_over
  const isMulti = !!(gameState?.multiplayer || effectivePlayerCount(config) >= 2)
  const p1Name = config.playerName || 'Player 1'
  const p2Name = config.playerName2 || 'Player 2'
  const currentLevel = gameState?.current_level ?? config.level
  const phase = gameState?.phase || (isOver ? 'session_end' : 'idle')
  const inputLocked = isInputBlocked(gameState)
  const overlayCountdownText = countdownDisplay(gameState)
  const phaseLabel = {
    idle: '● STARTING',
    playing: '● PLAYING',
    countdown: '● COUNTDOWN',
    level_clear: '● LEVEL CLEAR',
    level_fail: '● LEVEL FAIL',
    session_end: '● SESSION END',
  }[phase] || (isOver ? '● ENDED' : '● STARTING')

  return (
    <div className="simulator-container">
      <div className="simulator-header">
        <div>
          <h2 style={{ margin: 0 }}>
            {(config.game || 'hoops').toUpperCase()} - Level {currentLevel}
          </h2>
          <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
            {config.difficulty?.toUpperCase()}
          </span>
        </div>

        <div className="game-info">
          <button
            type="button"
            className="view-toggle-btn"
            onClick={() => setShowSim(v => !v)}
          >
            {showSim ? 'Show game board' : 'Show simulator'}
          </button>
          <button
            type="button"
            className="stop-game-btn"
            onClick={() => endGame('stopped')}
            disabled={stopping}
          >
            {stopping ? 'Stopping...' : '■ Stop Game'}
          </button>
        </div>
      </div>

      {/* Stable stage: iframe always full-size; HUD overlays on top */}
      <div className="simulator-stage">
        <iframe
          ref={iframeRef}
          className={`simulator-iframe ${showSim ? '' : 'simulator-iframe--hidden'}`}
          src={gameId ? `${WS_BRIDGE_URL}?game_id=${gameId}` : WS_BRIDGE_URL}
          title="Game Simulator"
        />

        {showPhaseOverlay(gameState) && phase === 'countdown' && !isOver && overlayCountdownText && (
          <div className="phase-countdown-overlay" aria-live="polite">
            <div className={`countdown-num${overlayCountdownText === 'GO!' ? ' go' : ''}`}>
              {overlayCountdownText}
            </div>
            <div className="countdown-meta">Level {currentLevel}</div>
          </div>
        )}

        {(phase === 'level_clear' || phase === 'level_fail') && !isOver && (
          <div className={`phase-overlay ${phase}-overlay`} aria-live="polite">
            {phase === 'level_clear' ? 'Level clear!' : 'Try again!'}
          </div>
        )}

        {!showSim && (
          <div className="play-hud">
            <div className="hud-board">
              <div className="hud-meta">
                <span className="hud-level">Level {currentLevel}</span>
                <span className="hud-diff">{config.difficulty?.toUpperCase()}</span>
                <span className={`hud-status ${isOver ? 'ended' : phase === 'playing' ? 'playing' : 'transition'}`}>
                  {isOver ? '● ENDED' : phaseLabel}
                </span>
              </div>

              <div className={`hud-players ${isMulti ? 'multi' : 'solo'}`}>
                <div className="hud-player">
                  <div className="hud-player-name">{p1Name}</div>
                  {config.minutesRemaining != null && (
                    <div className="hud-session-mins">
                      {Math.round(config.minutesRemaining)} min left
                    </div>
                  )}
                  <div className="hud-score">{gameState?.score ?? 0}</div>
                  <div className="hud-score-label">{isMulti ? 'P1 Score' : 'Score'}</div>
                </div>
                {isMulti && (
                  <div className="hud-player hud-player--p2">
                    <div className="hud-player-name">{p2Name}</div>
                    {config.minutesRemaining2 != null && (
                      <div className="hud-session-mins">
                        {Math.round(config.minutesRemaining2)} min left
                      </div>
                    )}
                    <div className="hud-score">{gameState?.score2 ?? 0}</div>
                    <div className="hud-score-label">P2 Score</div>
                  </div>
                )}
              </div>

              <div className="hud-stats">
                <div className="hud-stat">
                  <span
                    className="hud-stat-value"
                    style={{ color: timeLeft < 30 ? 'var(--color-error)' : 'var(--text-primary)' }}
                  >
                    {Math.max(0, timeLeft).toFixed(0)}s
                  </span>
                  <span className="hud-stat-label">Time Left</span>
                </div>
                <div className="hud-stat">
                  <HeartRow life={life} maxLife={maxLife} />
                  <span className="hud-stat-label">Lives {life}/{maxLife}</span>
                </div>
              </div>

              <button
                type="button"
                className="stop-game-btn stop-game-btn--lg"
                onClick={() => endGame('stopped')}
                disabled={stopping}
              >
                {stopping ? 'Stopping...' : '■ Stop Game'}
              </button>
            </div>
          </div>
        )}
      </div>

      {(config.playerName || config.playerName2) && (
        <div className="sim-player-bar">
          {config.playerName && (
            <span>
              {config.playerName}
              {config.minutesRemaining != null
                ? ` — ${Math.round(config.minutesRemaining)} min left`
                : ''}
            </span>
          )}
          {config.playerName2 && (
            <span className="sim-player-bar-p2">
              {config.playerName2}
              {config.minutesRemaining2 != null
                ? ` — ${Math.round(config.minutesRemaining2)} min left`
                : ''}
            </span>
          )}
        </div>
      )}

      {showSim && inputLocked && !isOver && (
        <div className="sim-input-lock" aria-hidden="true">
          Input paused ({phase})
        </div>
      )}

      {showSim && (
        <div className="sim-debug-footer">
          Game ID: {gameId} | P1: {config.cardId}
          {config.cardId2 ? ` | P2: ${config.cardId2}` : ''}
        </div>
      )}
    </div>
  )
}
