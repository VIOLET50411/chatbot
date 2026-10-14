import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { listCharacters, type OwnerCharacter } from '../../api';

const STORAGE_KEY = 'soul-mate-owner-character';

interface CharacterContextValue {
  characters: OwnerCharacter[];
  selected: OwnerCharacter | null;
  selectedId: string;
  select: (characterId: string) => void;
  isLoading: boolean;
  error: Error | null;
}

const CharacterContext = createContext<CharacterContextValue | null>(null);

export function CharacterProvider({ children }: { children: ReactNode }) {
  const charactersQuery = useQuery({
    queryKey: ['characters'],
    queryFn: ({ signal }) => listCharacters(signal),
  });
  const [selectedId, setSelectedId] = useState(() => localStorage.getItem(STORAGE_KEY) || '');
  const characters = charactersQuery.data ?? [];

  useEffect(() => {
    if (!characters.length) return;
    if (!characters.some((item) => item.id === selectedId)) {
      setSelectedId(characters[0].id);
    }
  }, [characters, selectedId]);

  const value = useMemo<CharacterContextValue>(() => {
    const selected = characters.find((item) => item.id === selectedId) ?? characters[0] ?? null;
    return {
      characters,
      selected,
      // 在人物目录尚未返回前保持空值，禁止角色作用域页面回退到默认人物。
      selectedId: selected?.id ?? '',
      select: (characterId: string) => {
        localStorage.setItem(STORAGE_KEY, characterId);
        setSelectedId(characterId);
      },
      isLoading: charactersQuery.isLoading,
      error: charactersQuery.error,
    };
  }, [characters, charactersQuery.error, charactersQuery.isLoading, selectedId]);

  return <CharacterContext.Provider value={value}>{children}</CharacterContext.Provider>;
}

export function useCharacter(): CharacterContextValue {
  const value = useContext(CharacterContext);
  if (!value) throw new Error('useCharacter 必须在 CharacterProvider 内使用');
  return value;
}
