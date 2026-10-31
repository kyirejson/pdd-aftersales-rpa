from pdd_rpa.desktop_executor import DesktopComponentExecutor


def test_wechat_sent_bubble_crop_tracks_lowest_right_side_green_bubble():
    class FakeImage:
        size = (500, 360)

        def __init__(self):
            self.box = None

        def load(self):
            class Pixels:
                def __getitem__(_self, point):
                    x, y = point
                    if 300 <= x <= 455 and 100 <= y <= 135:
                        return (157, 242, 159)
                    if 275 <= x <= 470 and 220 <= y <= 258:
                        return (157, 242, 159)
                    return (255, 255, 255)

            return Pixels()

        def crop(self, box):
            self.box = box
            return box

    image = FakeImage()
    crop = DesktopComponentExecutor._wechat_sent_bubble_crop(image, {})

    assert crop == image.box
    assert crop[0] < 275 < crop[2]
    assert crop[1] < 220 < crop[3]
    assert crop[3] > 258
