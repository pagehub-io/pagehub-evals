/**
 * ThemeContext stub. Mirrors app-prayers shape so AppHeader/DrawerContent
 * compile. Real theming + persistence lands later.
 *
 * Slice-2 added semantic verdict tokens (`success`/`danger`/`warning`)
 * so the runs verdict surface can visually distinguish pass / fail /
 * error. Values are GitHub Primer-ish and match the existing
 * `primary: '#1f6feb'` accent.
 */

import React, { createContext, useContext } from 'react';

export type Theme = {
  colors: {
    background: string;
    surface: string;
    text: string;
    textMuted: string;
    primary: string;
    border: string;
    success: string;
    danger: string;
    warning: string;
  };
};

const lightTheme: Theme = {
  colors: {
    background: '#ffffff',
    surface: '#f7f8fa',
    text: '#0b1220',
    textMuted: '#5b6478',
    primary: '#1f6feb',
    border: '#e2e6ee',
    success: '#1a7f37',
    danger: '#cf222e',
    warning: '#9a6700',
  },
};

type ThemeContextValue = {
  theme: Theme;
  isLoading: boolean;
};

const ThemeContext = createContext<ThemeContextValue>({ theme: lightTheme, isLoading: false });

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  return (
    <ThemeContext.Provider value={{ theme: lightTheme, isLoading: false }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}
