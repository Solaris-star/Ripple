import { api } from './ripple';

export interface RippleAuthUser {
  id: string; username: string; email?: string; role: 'owner' | 'admin' | 'member';
  workspace_id: string; workspace_name: string; local?: boolean;
}

export interface RippleAuthStatus {
  mode: 'local' | 'server'; local: boolean; setup_required: boolean; authenticated: boolean;
  user: RippleAuthUser | null;
  security: { configured: boolean; secure_cookie: boolean; public_origin: string; bootstrap_configured: boolean };
}

export const AUTH_CHANGED_EVENT = 'ripple:auth-changed';

export function fetchAuthStatus(): Promise<RippleAuthStatus> {
  return api<RippleAuthStatus>('/api/ripple/auth/status');
}
