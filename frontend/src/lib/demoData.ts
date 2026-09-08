import type { BookRecord } from './types'

const covers = {
  amber: 'linear-gradient(145deg, #d5a34d 0%, #8d3f27 62%, #321d1d 100%)',
  blue: 'linear-gradient(145deg, #4e8490 0%, #1d3e56 60%, #101827 100%)',
  moss: 'linear-gradient(145deg, #8e9b6b 0%, #3e594c 60%, #1e2929 100%)',
  plum: 'linear-gradient(145deg, #b36b73 0%, #56384f 55%, #211e2d 100%)',
  ink: 'linear-gradient(145deg, #7a7e89 0%, #343642 54%, #14151d 100%)',
  clay: 'linear-gradient(145deg, #d47a58 0%, #754236 58%, #291b1d 100%)',
}

export const DEMO_BOOKS: BookRecord[] = [
  { id: 'demo-quiet-architecture', title: 'The Quiet Architecture', subtitle: 'Notes on making a life with enough room in it', author: 'Mara Voss', description: 'A tender field guide to attention, domestic rituals, and the structures that make a day feel like it belongs to you.', language: 'English', year: '2024', pages: 248, category: 'Essays', readingTime: '4 hr 10 min', accent: covers.amber, sourceLabel: 'Folio demo catalogue', sourceKind: 'demo', documents: [], tags: ['Attention', 'Essays', 'Living'] },
  { id: 'demo-river-at-dusk', title: 'A River at Dusk', subtitle: 'Stories from the edge of the map', author: 'Jon Bell', description: 'Eight luminous stories about people who stay, leave, and find one another along a changing coast.', language: 'English', year: '2023', pages: 312, category: 'Fiction', readingTime: '5 hr 22 min', accent: covers.blue, sourceLabel: 'Folio demo catalogue', sourceKind: 'demo', documents: [], tags: ['Short fiction', 'Coastlines', 'Memory'] },
  { id: 'demo-wild-garden', title: 'Wild Garden, Patient Hands', subtitle: 'A seasonal notebook', author: 'Rina Das', description: 'An illustrated meditation on growing food, tending soil, and noticing the small economies of a garden.', language: 'English', year: '2022', pages: 176, category: 'Nature', readingTime: '2 hr 48 min', accent: covers.moss, sourceLabel: 'Folio demo catalogue', sourceKind: 'demo', documents: [], tags: ['Nature', 'Slow living', 'Craft'] },
  { id: 'demo-atlas-of-small-hours', title: 'Atlas of Small Hours', author: 'N. E. Okafor', description: 'A map of the night assembled from observatories, kitchens, train platforms, and the spaces between sleep.', language: 'English', year: '2021', pages: 284, category: 'Essays', readingTime: '4 hr 35 min', accent: covers.plum, sourceLabel: 'Folio demo catalogue', sourceKind: 'demo', documents: [], tags: ['Night', 'Maps', 'Culture'] },
  { id: 'demo-paper-moons', title: 'Paper Moons', subtitle: 'Letters from an unfinished city', author: 'Sofia Arendt', description: 'A collage of letters and fragments from a city still deciding what it wants to become.', language: 'English', year: '2020', pages: 208, category: 'Fiction', readingTime: '3 hr 31 min', accent: covers.ink, sourceLabel: 'Folio demo catalogue', sourceKind: 'demo', documents: [], tags: ['Letters', 'Cities', 'Fragments'] },
  { id: 'demo-before-the-weather', title: 'Before the Weather', author: 'Tomas Ilyin', description: 'How to read a landscape before it changes: a lucid introduction to wind, clouds, and weather memory.', language: 'English', year: '2024', pages: 224, category: 'Nature', readingTime: '3 hr 44 min', accent: covers.clay, sourceLabel: 'Folio demo catalogue', sourceKind: 'demo', documents: [], tags: ['Weather', 'Landscape', 'Science'] },
  { id: 'demo-the-long-listening', title: 'The Long Listening', author: 'Amina Hossain', description: 'Conversations about music, silence, and the shared spaces that let us hear more clearly.', language: 'English', year: '2019', pages: 264, category: 'Culture', readingTime: '4 hr 24 min', accent: covers.blue, sourceLabel: 'Folio demo catalogue', sourceKind: 'demo', documents: [], tags: ['Music', 'Listening', 'Culture'] },
  { id: 'demo-house-of-weather', title: 'House of Weather', author: 'Mara Voss', description: 'A novel of three generations, one house, and the weather that remembers every room.', language: 'English', year: '2018', pages: 336, category: 'Fiction', readingTime: '5 hr 48 min', accent: covers.amber, sourceLabel: 'Folio demo catalogue', sourceKind: 'demo', documents: [], tags: ['Family', 'Weather', 'Home'] },
]

export const DEMO_CATEGORIES = ['Essays', 'Fiction', 'Nature', 'Culture']

export function findDemoBook(id: string): BookRecord | undefined {
  return DEMO_BOOKS.find((book) => book.id === id)
}

export function searchDemoBooks(query: string): BookRecord[] {
  const normalized = query.trim().toLocaleLowerCase()
  if (!normalized) return DEMO_BOOKS
  return DEMO_BOOKS.filter((book) => [book.title, book.subtitle, book.author, book.category, ...book.tags].filter(Boolean).join(' ').toLocaleLowerCase().includes(normalized))
}
