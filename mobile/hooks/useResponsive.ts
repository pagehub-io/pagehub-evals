import { useWindowDimensions } from 'react-native';

const MOBILE_MAX = 768;
const TABLET_MAX = 1024;

export function useIsMobile() {
  const { width } = useWindowDimensions();
  return width <= MOBILE_MAX;
}

export function useIsMedium() {
  const { width } = useWindowDimensions();
  return width > MOBILE_MAX && width <= TABLET_MAX;
}

export function useIsDesktop() {
  const { width } = useWindowDimensions();
  return width > TABLET_MAX;
}

/** Drawer auto-opens on medium-and-up per the fleet pattern. */
export function useShouldAutoOpenDrawer() {
  const { width } = useWindowDimensions();
  return width > MOBILE_MAX;
}
