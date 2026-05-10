/**
 * ThemeContext stub. Mirrors app-prayers shape so AppHeader/DrawerContent
 * compile. Real theming + persistence lands later.
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
