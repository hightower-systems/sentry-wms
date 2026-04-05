import React, { useState } from 'react';
import { View, Text, TouchableOpacity, StyleSheet } from 'react-native';
import { colors, fonts } from '../theme/styles';

export default function PagedList({ items, pageSize = 20, renderItem }) {
  const [page, setPage] = useState(0);
  const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
  const start = page * pageSize;
  const pageItems = items.slice(start, start + pageSize);

  return (
    <View style={styles.container}>
      <View style={styles.list}>
        {pageItems.map((item, index) => (
          <View key={index}>{renderItem(item, start + index)}</View>
        ))}
      </View>
      {totalPages > 1 && (
        <View style={styles.pager}>
          <TouchableOpacity
            style={styles.pageButton}
            onPress={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
          >
            <Text style={[styles.pageArrow, page === 0 && styles.pageArrowDisabled]}>{'<'}</Text>
          </TouchableOpacity>
          <Text style={styles.pageText}>
            Page {page + 1} of {totalPages}
          </Text>
          <TouchableOpacity
            style={styles.pageButton}
            onPress={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            disabled={page >= totalPages - 1}
          >
            <Text style={[styles.pageArrow, page >= totalPages - 1 && styles.pageArrowDisabled]}>
              {'>'}
            </Text>
          </TouchableOpacity>
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
  },
  list: {
    flex: 1,
  },
  pager: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    paddingVertical: 12,
    gap: 16,
  },
  pageButton: {
    padding: 8,
    minWidth: 48,
    minHeight: 48,
    alignItems: 'center',
    justifyContent: 'center',
  },
  pageArrow: {
    fontFamily: fonts.mono,
    fontSize: 18,
    fontWeight: '700',
    color: colors.textPrimary,
  },
  pageArrowDisabled: {
    color: colors.border,
  },
  pageText: {
    fontFamily: fonts.mono,
    fontSize: 13,
    color: colors.textMuted,
  },
});
