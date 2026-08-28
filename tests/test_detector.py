import unittest

import cv2
import numpy as np

from gomoku_assistant.detector import DetectionError, grid_liveness, locate_board, read_stones
from gomoku_assistant.engine import BLACK, EMPTY, WHITE


def synthetic_screen() -> np.ndarray:
    image = np.full((720, 1040, 3), (239, 241, 244), dtype=np.uint8)
    left, top, side, margin = 260, 80, 560, 30
    image[top : top + side, left : left + side] = (213, 163, 91)
    lines = np.linspace(left + margin, left + side - margin, 15).astype(int)
    for coordinate in lines:
        cv2.line(image, (coordinate, top + margin), (coordinate, top + side - margin), (58, 50, 42), 2)
        cv2.line(image, (left + margin, top + coordinate - left), (left + side - margin, top + coordinate - left), (58, 50, 42), 2)
    cv2.circle(image, (lines[7], top + margin + (lines[8] - lines[0])), 15, (23, 24, 26), -1)
    cv2.circle(image, (lines[9], top + margin + (lines[6] - lines[0])), 15, (246, 246, 242), -1)
    cv2.circle(image, (lines[9], top + margin + (lines[6] - lines[0])), 15, (130, 130, 125), 2)
    return image


def synthetic_marked_white_screen() -> np.ndarray:
    image = synthetic_screen()
    left, top, side, margin = 260, 80, 560, 30
    lines = np.linspace(left + margin, left + side - margin, 15).astype(int)
    x = lines[5]
    y = top + margin + (lines[10] - lines[0])
    cv2.circle(image, (x, y), 15, (246, 246, 242), -1)
    cv2.circle(image, (x, y), 15, (130, 130, 125), 2)
    cv2.line(image, (x - 9, y), (x + 9, y), (225, 48, 48), 3)
    cv2.line(image, (x, y - 9), (x, y + 9), (225, 48, 48), 3)
    return image


def synthetic_portrait_board() -> np.ndarray:
    image = np.full((940, 505, 3), (82, 55, 33), dtype=np.uint8)
    left, top, side, margin = 6, 248, 493, 14
    image[top : top + side, left : left + side] = (221, 171, 105)
    lines = np.linspace(left + margin, left + side - margin - 1, 15).astype(int)
    y_lines = np.linspace(top + margin, top + side - margin - 1, 15).astype(int)
    for coordinate in lines:
        cv2.line(image, (coordinate, y_lines[0]), (coordinate, y_lines[-1]), (118, 83, 55), 2)
    for coordinate in y_lines:
        cv2.line(image, (lines[0], coordinate), (lines[-1], coordinate), (118, 83, 55), 2)
    return image


class DetectorTests(unittest.TestCase):
    def test_locates_grid_and_reads_stones(self) -> None:
        image = synthetic_screen()
        geometry = locate_board(image, 15)
        self.assertGreater(geometry.confidence, 0.5)
        board, _ = read_stones(image, geometry)
        self.assertEqual(board[8, 7], BLACK)
        self.assertEqual(board[6, 9], WHITE)
        self.assertEqual(board[7, 7], EMPTY)

    def test_reads_white_stone_with_red_last_move_marker(self) -> None:
        image = synthetic_marked_white_screen()
        geometry = locate_board(image, 15)
        board, _ = read_stones(image, geometry)
        self.assertEqual(board[10, 5], WHITE)

    def test_locates_portrait_warm_board(self) -> None:
        image = synthetic_portrait_board()

        geometry = locate_board(image, 15)

        self.assertLessEqual(
            max(abs(a - b) for a, b in zip(geometry.bounds, (20, 262, 484, 726))),
            3,
        )
        self.assertGreater(grid_liveness(image, geometry), 0.8)

    def test_grid_liveness_rejects_same_size_uniform_frame(self) -> None:
        image = synthetic_screen()
        geometry = locate_board(image, 15)
        uniform = np.full_like(image, (210, 170, 110))

        self.assertGreater(grid_liveness(image, geometry), 0.8)
        self.assertLess(grid_liveness(uniform, geometry), 0.1)

    def test_grid_liveness_rejects_translated_board(self) -> None:
        image = synthetic_screen()
        geometry = locate_board(image, 15)
        translated = cv2.warpAffine(
            image,
            np.float32([[1, 0, 8], [0, 1, 8]]),
            (image.shape[1], image.shape[0]),
            borderValue=(239, 241, 244),
        )

        self.assertLess(grid_liveness(translated, geometry), 0.4)

    def test_prefers_square_board_over_larger_rectangular_lattice(self) -> None:
        image = np.full((1000, 3000, 3), (145, 170, 190), dtype=np.uint8)

        false_x = np.linspace(960, 1990, 15).astype(int)
        false_y = np.linspace(110, 870, 15).astype(int)
        image[30:970, 900:2050] = (210, 210, 210)
        for x in false_x:
            cv2.line(image, (x, false_y[0]), (x, false_y[-1]), (70, 70, 70), 2)
        for y in false_y:
            cv2.line(image, (false_x[0], y), (false_x[-1], y), (70, 70, 70), 2)

        left, top, side, margin = 2430, 220, 500, 19
        image[top : top + side, left : left + side] = (218, 178, 112)
        true_x = np.linspace(left + margin, left + side - margin, 15).astype(int)
        true_y = np.linspace(top + margin, top + side - margin, 15).astype(int)
        for x in true_x:
            cv2.line(image, (x, true_y[0]), (x, true_y[-1]), (58, 50, 42), 2)
        for y in true_y:
            cv2.line(image, (true_x[0], y), (true_x[-1], y), (58, 50, 42), 2)

        geometry = locate_board(image, 15)
        expected = (true_x[0], true_y[0], true_x[-1], true_y[-1])
        self.assertLessEqual(max(abs(a - b) for a, b in zip(geometry.bounds, expected)), 2)

    def test_rejects_rectangular_pseudo_grid(self) -> None:
        image = np.full((720, 1040, 3), (239, 241, 244), dtype=np.uint8)
        x_lines = np.linspace(300, 720, 15).astype(int)
        y_lines = np.linspace(200, 520, 15).astype(int)
        for x in x_lines:
            cv2.line(image, (x, y_lines[0]), (x, y_lines[-1]), (58, 50, 42), 2)
        for y in y_lines:
            cv2.line(image, (x_lines[0], y), (x_lines[-1], y), (58, 50, 42), 2)

        with self.assertRaises(DetectionError):
            locate_board(image, 15)


if __name__ == "__main__":
    unittest.main()
