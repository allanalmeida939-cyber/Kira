import { maskEmail, presentIdentityEmail, sanitizeIdentitySnapshot } from './apiIdentityPresentation.js';

globalThis.KiraApiIdentityPresentation = Object.freeze({
  maskEmail,
  presentIdentityEmail,
  sanitizeIdentitySnapshot,
});
