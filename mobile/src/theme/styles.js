import { Platform, StyleSheet } from 'react-native';

export const colors = {
  // Brand
  accentRed: '#8e2716',
  copper: '#b87333',
  cream: '#fdf4e3',

  // Surfaces
  background: '#ffffff',
  cardBg: '#f7f3ec',
  cardBorder: '#e0d9cc',
  inputBg: '#f7f3ec',
  inputBorder: '#d6cfc0',

  // Text
  textPrimary: '#1a1a1a',
  textSecondary: '#7a7060',
  textMuted: '#999080',
  textPlaceholder: '#b0a898',

  // Status
  success: '#34a853',
  warning: '#b87333',
  danger: '#8e2716',

  // Utility
  border: '#e0d9cc',
  overlay: 'rgba(0,0,0,0.4)',
  grayAccent: '#a09b91',
};

export const radii = {
  card: 12,
  input: 12,
  button: 12,
  badge: 6,
  small: 8,
  heroCard: 12,
};

export const spacing = {
  screenPadding: 16,
  cardGap: 8,
  sectionGap: 12,
  cardPadding: 14,
  bottomBarPadding: 16,
};

export const fonts = {
  mono: Platform.select({ ios: 'Menlo', android: 'monospace', default: 'monospace' }),
};

// ── Shared screen layout ─────────────────────────────────────
export const screenStyles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.background },
  header: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between',
    paddingHorizontal: 16, paddingTop: 52, paddingBottom: 12,
  },
  backBtn: { padding: 4, minWidth: 32, minHeight: 48, justifyContent: 'center' },
  backText: { fontSize: 22, color: colors.textPrimary },
  headerTitle: {
    fontFamily: fonts.mono, fontSize: 16, fontWeight: '700',
    color: colors.textPrimary, letterSpacing: 0.5, textTransform: 'uppercase',
  },
  content: { flex: 1 },
  contentInner: { padding: 16 },
  bottomBar: { padding: 16, borderTopWidth: 1, borderTopColor: colors.cardBorder, gap: 8, flexDirection: 'row' },
  menuBtn: { padding: 4, minWidth: 32, minHeight: 48, justifyContent: 'center', alignItems: 'center' },
  menuIcon: { fontSize: 20, color: colors.textPrimary, fontWeight: '700' },
});

// ── Shared buttons ───────────────────────────────────────────
export const buttonStyles = StyleSheet.create({
  buttonPrimary: {
    backgroundColor: colors.accentRed, borderRadius: radii.button,
    paddingVertical: 14, alignItems: 'center', minHeight: 48,
  },
  buttonPrimaryText: {
    color: colors.cream, fontFamily: fonts.mono, fontSize: 14,
    fontWeight: '700', letterSpacing: 0.5, textTransform: 'uppercase',
  },
  buttonSecondary: {
    backgroundColor: colors.background, borderWidth: 1.5, borderColor: colors.cardBorder,
    borderRadius: radii.button, paddingVertical: 14, alignItems: 'center', minHeight: 48,
  },
  buttonSecondaryText: {
    color: colors.textSecondary, fontFamily: fonts.mono, fontSize: 14,
    fontWeight: '600', letterSpacing: 0.5, textTransform: 'uppercase',
  },
  buttonDisabled: { opacity: 0.5 },
});

// ── Shared modals ────────────────────────────────────────────
export const modalStyles = StyleSheet.create({
  overlay: {
    flex: 1, backgroundColor: colors.overlay,
    justifyContent: 'center', alignItems: 'center', padding: 32,
  },
  card: {
    backgroundColor: colors.background, borderRadius: radii.card, padding: 24,
    width: '100%', maxWidth: 320, borderWidth: 1, borderColor: colors.cardBorder,
  },
  title: {
    fontFamily: fonts.mono, fontSize: 16, fontWeight: '700',
    color: colors.textPrimary, marginBottom: 8,
  },
  subtitle: { fontSize: 13, color: colors.textMuted, marginBottom: 16 },
  divider: { height: 1, backgroundColor: colors.cardBorder, marginVertical: 16 },
  body: { fontSize: 14, color: colors.textPrimary, marginBottom: 20 },
  actions: { gap: 8, flexDirection: 'row' },
});

// ── Shared list row patterns ─────────────────────────────────
export const listStyles = StyleSheet.create({
  row: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between',
    backgroundColor: colors.cardBg, borderWidth: 1, borderColor: colors.cardBorder,
    borderRadius: radii.card, padding: 12, marginBottom: 8, minHeight: 48,
  },
  removeBtn: { padding: 8, minWidth: 48, minHeight: 48, alignItems: 'center', justifyContent: 'center' },
  removeText: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '700', color: colors.textMuted },
  sku: { fontFamily: fonts.mono, fontSize: 14, fontWeight: '600', color: colors.textPrimary },
  itemName: { fontSize: 12, color: colors.textMuted, marginTop: 2 },
  label: {
    fontFamily: fonts.mono, fontSize: 10, fontWeight: '600',
    color: colors.textMuted, letterSpacing: 0.3, marginBottom: 2,
    textTransform: 'uppercase',
  },
  qtyInput: {
    fontFamily: fonts.mono, fontSize: 18, fontWeight: '700', color: colors.textPrimary,
    backgroundColor: colors.inputBg, borderWidth: 1, borderColor: colors.inputBorder,
    borderRadius: radii.input, paddingHorizontal: 12, paddingVertical: 8,
    width: 80, textAlign: 'center', minHeight: 48,
  },
});

// ── Shared done / success section ────────────────────────────
export const doneStyles = StyleSheet.create({
  section: { flex: 1, justifyContent: 'center', alignItems: 'center', padding: 32 },
  check: { fontSize: 64, color: colors.success, marginBottom: 16 },
  title: {
    fontFamily: fonts.mono, fontSize: 22, fontWeight: '700',
    color: colors.textPrimary, marginBottom: 8,
  },
  detail: { fontSize: 15, color: colors.textMuted, marginBottom: 32 },
});

export default StyleSheet.create({
  // ── Layout ──────────────────────────────────────────────
  screen: {
    flex: 1,
    backgroundColor: colors.background,
  },
  screenContent: {
    flex: 1,
    padding: spacing.screenPadding,
  },

  // ── Header ──────────────────────────────────────────────
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
    paddingTop: 52,
    paddingBottom: 12,
  },
  headerTitle: {
    fontFamily: fonts.mono,
    fontSize: 16,
    fontWeight: '700',
    color: colors.textPrimary,
    letterSpacing: 0.5,
    textTransform: 'uppercase',
  },
  headerBack: {
    paddingRight: 12,
    paddingVertical: 4,
  },
  headerBackText: {
    fontSize: 22,
    color: colors.textPrimary,
  },

  // ── Cards ───────────────────────────────────────────────
  card: {
    backgroundColor: colors.cardBg,
    borderWidth: 1,
    borderColor: colors.cardBorder,
    borderRadius: radii.card,
    padding: spacing.cardPadding,
    marginBottom: spacing.sectionGap,
  },
  cardRed: {
    backgroundColor: colors.cardBg,
    borderWidth: 1.5,
    borderColor: colors.accentRed,
    borderRadius: radii.card,
    padding: spacing.cardPadding,
    marginBottom: spacing.sectionGap,
  },

  // ── Buttons ─────────────────────────────────────────────
  buttonPrimary: {
    backgroundColor: colors.accentRed,
    borderRadius: radii.button,
    paddingVertical: 14,
    paddingHorizontal: 24,
    alignItems: 'center',
    justifyContent: 'center',
    minHeight: 48,
  },
  buttonPrimaryText: {
    color: colors.cream,
    fontFamily: fonts.mono,
    fontSize: 14,
    fontWeight: '700',
    letterSpacing: 0.5,
    textTransform: 'uppercase',
  },
  buttonSecondary: {
    backgroundColor: colors.background,
    borderWidth: 1.5,
    borderColor: colors.cardBorder,
    borderRadius: radii.button,
    paddingVertical: 14,
    paddingHorizontal: 24,
    alignItems: 'center',
    justifyContent: 'center',
    minHeight: 48,
  },
  buttonSecondaryText: {
    color: colors.textSecondary,
    fontFamily: fonts.mono,
    fontSize: 14,
    fontWeight: '600',
    letterSpacing: 0.5,
    textTransform: 'uppercase',
  },
  buttonDisabled: {
    opacity: 0.5,
  },

  // ── Scan Input ──────────────────────────────────────────
  scanInputContainer: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: colors.inputBg,
    borderWidth: 1.5,
    borderColor: colors.inputBorder,
    borderRadius: radii.input,
    paddingHorizontal: 12,
    minHeight: 44,
    marginBottom: 16,
  },
  scanInputField: {
    flex: 1,
    fontFamily: fonts.mono,
    fontSize: 12,
    color: colors.textPrimary,
    letterSpacing: 1,
    paddingVertical: 10,
  },
  scanInputDisabled: {
    backgroundColor: '#f0ede6',
    borderColor: colors.cardBorder,
  },

  // ── Badges ──────────────────────────────────────────────
  badge: {
    backgroundColor: colors.accentRed,
    borderRadius: 10,
    paddingHorizontal: 8,
    paddingVertical: 2,
    minWidth: 24,
    alignItems: 'center',
  },
  badgeCopper: {
    backgroundColor: colors.copper,
  },
  badgeText: {
    color: colors.cream,
    fontFamily: fonts.mono,
    fontSize: 10,
    fontWeight: '700',
  },

  // ── Typography ──────────────────────────────────────────
  monoText: {
    fontFamily: fonts.mono,
  },
  sku: {
    fontFamily: fonts.mono,
    fontSize: 14,
    color: colors.textPrimary,
  },
  binCode: {
    fontFamily: fonts.mono,
    fontSize: 30,
    fontWeight: '700',
    color: colors.accentRed,
  },
  qty: {
    fontFamily: fonts.mono,
    fontSize: 28,
    fontWeight: '700',
    color: colors.accentRed,
  },
  label: {
    fontFamily: fonts.mono,
    fontSize: 10,
    fontWeight: '600',
    color: colors.textMuted,
    letterSpacing: 0.3,
    textTransform: 'uppercase',
    marginBottom: 2,
  },
  itemName: {
    fontSize: 14,
    color: colors.textPrimary,
  },
  subtitle: {
    fontFamily: fonts.mono,
    fontSize: 12,
    color: colors.copper,
    letterSpacing: 0.3,
    textTransform: 'uppercase',
  },
  muted: {
    fontSize: 12,
    color: colors.textMuted,
  },

  // ── Form Inputs ─────────────────────────────────────────
  textInput: {
    borderWidth: 1,
    borderColor: colors.inputBorder,
    borderRadius: radii.input,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 14,
    color: colors.textPrimary,
    backgroundColor: colors.inputBg,
    minHeight: 48,
  },
  quantityInput: {
    borderWidth: 1,
    borderColor: colors.inputBorder,
    borderRadius: radii.input,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontFamily: fonts.mono,
    fontSize: 18,
    fontWeight: '700',
    color: colors.textPrimary,
    backgroundColor: colors.inputBg,
    minHeight: 48,
    textAlign: 'center',
    width: 80,
  },

  // ── List Items ──────────────────────────────────────────
  listItem: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    backgroundColor: colors.cardBg,
    borderWidth: 1,
    borderColor: colors.cardBorder,
    borderRadius: radii.card,
    paddingVertical: 12,
    paddingHorizontal: 16,
    marginBottom: 8,
    minHeight: 48,
  },

  // ── Misc ────────────────────────────────────────────────
  divider: {
    height: 1,
    backgroundColor: colors.cardBorder,
    marginVertical: 12,
  },
  centerContent: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    padding: 24,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
  },
  spaceBetween: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
  },
});
