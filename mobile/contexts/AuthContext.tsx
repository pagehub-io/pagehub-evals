/**
 * AuthContext stub.
 *
 * Real impl mirrors app-prayers/mobile/contexts/AuthContext.tsx:
 *   - reads pagehub-auth-issued JWT from tokenStorage
 *   - decodes claims (app_slug, sub, email)
 *   - exposes isAuthenticated / user / consumeReturnTo
 *
 * In this scaffold we expose the same shape so dependent components
 * compile. The implementation is filled in alongside the JTBD pivot.
 */

import React, { createContext, useContext, useState } from 'react';

type User = { id: string; email: string } | null;

type AuthContextValue = {
  isAuthenticated: boolean;
  isLoading: boolean;
  user: User;
  consumeReturnTo: () => string | null;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user] = useState<User>(null);
  const [isLoading] = useState(false);

  const value: AuthContextValue = {
    isAuthenticated: user !== null,
    isLoading,
    user,
    consumeReturnTo: () => null,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider');
  return ctx;
}
