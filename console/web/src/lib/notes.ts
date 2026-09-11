export const NOTE_CATEGORIES = ["general", "findings", "methodology", "questions", "plan", "wiki"] as const;
export type NoteCategory = typeof NOTE_CATEGORIES[number];

export interface NoteAuthor {
  agent_id: string | null;
  agent_name: string | null;
}

export interface NoteMetadata {
  note_id: string;
  title: string;
  category: NoteCategory;
  tags: string[];
  revision: number;
  created_by: NoteAuthor | null;
  updated_by: NoteAuthor | null;
  created_at: string | null;
  updated_at: string | null;
  deleted: boolean;
  deleted_at: string | null;
  deleted_by: NoteAuthor | null;
}

export interface NotePreview extends NoteMetadata {
  preview: string;
  content_length: number;
  history_truncated: boolean;
}

export interface NoteRevision extends NoteMetadata {
  content: string;
}

export interface SharedNote extends NoteRevision {
  history_truncated: boolean;
  history?: NoteRevision[];
}

export interface NotesPage {
  success: boolean;
  source_status: "available" | "missing" | "unreadable";
  error_code?: string;
  notes: NotePreview[];
  total: number | null;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface NoteDetail {
  success: boolean;
  source_status: "available" | "missing" | "unreadable";
  error_code?: string;
  note?: SharedNote;
}
