import axios from 'axios';

// 支持独立部署：前端在 Vercel，后端在其它平台时通过 VITE_API_BASE_URL 指向后端
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL
  ? `${import.meta.env.VITE_API_BASE_URL}/api`
  : '/api';

const api = axios.create({ baseURL: API_BASE_URL });

// Matches
export const fetchMatches = (params?: { status?: string; game?: string; team?: string; days?: number }) =>
  api.get('/matches', { params }).then(r => r.data);

export const fetchMatch = (matchId: string) =>
  api.get(`/matches/${matchId}`).then(r => r.data);

export const fetchMatchPrices = (matchId: string, hours?: number) =>
  api.get(`/matches/${matchId}/prices`, { params: hours ? { hours } : {} }).then(r => r.data);

export const fetchMatchOrderbook = (matchId: string) =>
  api.get(`/matches/${matchId}/orderbook`).then(r => r.data);

// Trades
export const fetchTrades = (params?: { settled?: number; game?: string; signal?: string; team?: string; days?: number }) =>
  api.get('/trades', { params }).then(r => r.data);

export const fetchTradeStats = (days?: number) =>
  api.get('/trades/stats', { params: days ? { days } : {} }).then(r => r.data);

export const fetchTradeStatsGrouped = (groupBy: string, days?: number) =>
  api.get('/trades/stats/grouped', { params: { group_by: groupBy, ...(days ? { days } : {}) } }).then(r => r.data);

export const fetchTrade = (tradeId: number) =>
  api.get(`/trades/${tradeId}`).then(r => r.data);

// Signals
export const fetchSignals = (params?: { match_id?: string; signal_name?: string; days?: number }) =>
  api.get('/signals', { params }).then(r => r.data);

export const fetchSignalTypes = () =>
  api.get('/signals/types').then(r => r.data);

export const fetchSignalSeries = (signalId: number) =>
  api.get(`/signals/${signalId}/series`).then(r => r.data);

// System
export const fetchSystemStatus = () =>
  api.get('/system/status').then(r => r.data);
