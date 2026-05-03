from PIL import Image
import subprocess
import cv2
import numpy as np

def _preprocess_cell(pil_cell: 'Image.Image', scale=5, pad=10, stroke_thickness=5):
    # 1. Scaling + Early Erosion to separate touching pixels
    img = np.array(pil_cell.convert("L"))
    upscaled = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4)
    eroded = cv2.erode(upscaled, np.ones((3, 3), np.uint8), iterations=1)
    _, mask = cv2.threshold(eroded, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # 2. Contour Extraction & Sorting
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)
    
    bw = np.ones_like(mask) * 255
    img_h, img_w = mask.shape
    image_area = img_h * img_w
    shells = [] 

    for cnt in contours:
        area = cv2.contourArea(cnt)
        x, y, w, h = cv2.boundingRect(cnt)
        
        # A: Ignore the image border
        if area > (image_area * 0.9):
          continue

        # B: Spatial Containment (Check if inside a Zero or Normal digit)
        parent_shell = None
        for s in shells:
            sx, sy, sw, sh = s['box']
            if x >= sx-2 and y >= sy-2 and (x+w) <= (sx+sw+2) and (y+h) <= (sy+sh+2):
                parent_shell = s
                break
        
        # C: Suppression: Don't draw the 'slash' inside a Zero
        if parent_shell and parent_shell['type'] == 'zero':
            continue

        # D: Normalization for scoring
        norm_scale = 100.0 / h if h > 0 else 1
        cnt_norm = ((cnt.astype(np.float32) - [x, y]) * norm_scale).astype(np.float32)

        ellipse_score = 0
        if len(cnt_norm) >= 5:
            _, (MA, ma), _ = cv2.fitEllipse(cnt_norm)
            ellipse_area = (np.pi * MA * ma) / 4.0
            ellipse_score = cv2.contourArea(cnt_norm) / ellipse_area if ellipse_area > 0 else 0

        # E: Solidity Check (Zero = High, 8 = Low due to waist)
        hull = cv2.convexHull(cnt)
        hull_area = cv2.contourArea(hull)
        solidity = area / hull_area if hull_area > 0 else 0

        # G: Hybrid Rendering
        if parent_shell:
            # Hole in 9, 8, etc -> Fill White
            cv2.drawContours(bw, [cnt], -1, 255, thickness=-1)
        else:
            # New Shell -> Determine if it's a 0 or a normal digit
            if ellipse_score > 0.88 and solidity > 0.94:
                cv2.drawContours(bw, [cnt], -1, 0, stroke_thickness)
                shells.append({'box': (x, y, w, h), 'type': 'zero'})
            else:
                cv2.drawContours(bw, [cnt], -1, 0, thickness=-1)
                shells.append({'box': (x, y, w, h), 'type': 'normal'})

    # 3. Final Polish: Padding + Dilation (thins the black text)
    bw = cv2.copyMakeBorder(bw, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
    bw = cv2.dilate(bw, np.ones((3, 3), np.uint8), iterations=1) 
    
    return bw

def test_on_file(file_path, coords):
    """
    Crops the image and shows the original vs. Otsu-Contour-Stroke version.
    """
    x1, y1, x2, y2 = coords
    full_img = Image.open(file_path)
    cell_img = full_img.crop((x1, y1, x2, y2))
    
    cleaned_img = _preprocess_cell(cell_img)

    # Convert to BGR for OpenCV display
    original_cv = cv2.cvtColor(np.array(cell_img), cv2.COLOR_RGB2BGR)
    
    cv2.imshow("Original (Cropped)", original_cv)
    cv2.imshow("Otsu + External Stroke", cleaned_img)
    
    success, buffer = cv2.imencode(".png", cleaned_img)
    if not success:
        raise ValueError("Could not encode image")

    image_bytes = buffer.tobytes()

    
    cmd = [
        "tesseract",
        "stdin",
        "stdout",
        "--psm", "7",
        "--oem", "3",
        "-c", "load_system_dawg=0",
        "-c", "load_freq_dawg=0",
        "-c", "tessedit_char_whitelist=0123456789",
        "tsv"
    ]

    out = subprocess.check_output(
        cmd,
        input=image_bytes
    )
    print(out.decode(errors='ignore'))
      
    
    print(f"Testing file: {file_path}")
    print("Press any key to close the windows...")
    cv2.waitKey(0)
    cv2.destroyAllWindows()

# --- RUN TEST ---
# Note: Ensure coordinates match the digit location in your specific file
test_on_file('live_720p.png', (1170, 665, 1195, 685))
test_on_file('live_720p.png', (81, 585, 312, 640)) #long
