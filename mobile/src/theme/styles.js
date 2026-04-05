import { Platform, StyleSheet } from 'react-native';

export const colors = {
  accentRed: '#8e2715',
  copper: '#c4722a',
  cream: '#FCF4E3',
  background: '#FFFFFF',
  border: '#d5d0c8',
  textPrimary: '#1A1714',
  textSecondary: '#b0a99e',
  textMuted: '#999999',
  overlay: 'rgba(0, 0, 0, 0.5)',
  success: '#2d7a3a',
  warning: '#c4722a',
};

export const fonts = {
  mono: Platform.select({ ios: 'Menlo', android: 'monospace', default: 'monospace' }),
};

export default StyleSheet.create({
  // ── Layout ──────────────────────────────────────────────
  screen: {
    flex: 1,
    backgroundColor: colors.background,
  },
  screenContent: {
    flex: 1,
    padding: 16,
  },

  // ── Header ──────────────────────────────────────────────
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
    paddingVertical: 12,
    backgroundColor: colors.background,
    borderBottomWidth: 2,
    borderBottomColor: colors.accentRed,
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
    backgroundColor: colors.background,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 8,
    padding: 16,
    marginBottom: 12,
  },
  cardRed: {
    backgroundColor: colors.background,
    borderWidth: 1.5,
    borderColor: colors.accentRed,
    borderRadius: 8,
    padding: 16,
    marginBottom: 12,
  },

  // ── Buttons ─────────────────────────────────────────────
  buttonPrimary: {
    backgroundColor: colors.accentRed,
    borderRadius: 8,
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
    borderColor: colors.border,
    borderRadius: 8,
    paddingVertical: 14,
    paddingHorizontal: 24,
    alignItems: 'center',
    justifyContent: 'center',
    minHeight: 48,
  },
  buttonSecondaryText: {
    color: colors.textMuted,
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
    backgroundColor: colors.background,
    borderWidth: 1.5,
    borderColor: colors.accentRed,
    borderRadius: 8,
    paddingHorizontal: 12,
    minHeight: 48,
    marginBottom: 16,
  },
  scanInputIcon: {
    fontFamily: fonts.mono,
    fontSize: 18,
    color: colors.accentRed,
    marginRight: 8,
  },
  scanInputField: {
    flex: 1,
    fontFamily: fonts.mono,
    fontSize: 14,
    color: colors.textPrimary,
    paddingVertical: 12,
  },
  scanInputDisabled: {
    backgroundColor: '#f5f5f5',
    borderColor: colors.border,
  },

  // ── Function Rows (Home screen) ─────────────────────────
  functionRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    backgroundColor: colors.background,
    borderWidth: 1,
    borderColor: colors.border,
    borderLeftWidth: 3,
    borderRadius: 8,
    paddingVertical: 16,
    paddingHorizontal: 16,
    marginBottom: 8,
    minHeight: 48,
  },
  functionRowRed: {
    borderLeftColor: colors.accentRed,
  },
  functionRowCopper: {
    borderLeftColor: colors.copper,
  },
  functionRowGray: {
    borderLeftColor: colors.border,
  },
  functionLabel: {
    fontFamily: fonts.mono,
    fontSize: 14,
    fontWeight: '700',
    color: colors.textPrimary,
    letterSpacing: 0.5,
    textTransform: 'uppercase',
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
    color: '#FFFFFF',
    fontFamily: fonts.mono,
    fontSize: 12,
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
    borderColor: colors.border,
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 14,
    color: colors.textPrimary,
    backgroundColor: colors.background,
    minHeight: 48,
  },
  quantityInput: {
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontFamily: fonts.mono,
    fontSize: 18,
    fontWeight: '700',
    color: colors.textPrimary,
    backgroundColor: colors.background,
    minHeight: 48,
    textAlign: 'center',
    width: 80,
  },

  // ── List Items ──────────────────────────────────────────
  listItem: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    backgroundColor: colors.background,
    borderWidth: 1,
    borderColor: colors.border,
    borderRadius: 8,
    paddingVertical: 12,
    paddingHorizontal: 16,
    marginBottom: 8,
    minHeight: 48,
  },

  // ── Misc ────────────────────────────────────────────────
  divider: {
    height: 1,
    backgroundColor: colors.border,
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
