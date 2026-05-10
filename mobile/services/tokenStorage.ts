/**
 * Token storage stub. Mirrors app-prayers shape so SupportWidget compiles.
 * Real impl uses expo-secure-store on native + localStorage on web.
 */

const memoryStore: Record<string, string | null> = {};

export const tokenStorage = {
  async getItem(key: string): Promise<string | null> {
    return memoryStore[key] ?? null;
  },
  async setItem(key: string, value: string): Promise<void> {
    memoryStore[key] = value;
  },
  async removeItem(key: string): Promise<void> {
    memoryStore[key] = null;
  },
};
