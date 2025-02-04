#!/usr/bin/env python3

import cv2
import numpy as np
import os
from skimage import img_as_float32, img_as_uint
from skimage.color import rgb2lab, lab2rgb
from skimage.filters import gaussian
from skimage.registration import phase_cross_correlation
import PIL.Image
import PIL.ImageCms
import cupy as cp  # GPU array operations
# Initialize GPU device
cp.cuda.Device(0).use()

# Create reusable kernels for common operations
laplace_kernel = cp.array([[0, 1, 0],
                          [1, -4, 1],
                          [0, 1, 0]], dtype=cp.float32)

# Ultra-sharp kernel with extreme micro-detail preservation
sharp_kernel = cp.array([[-4,-4,-4],
                        [-4, 33,-4],
                        [-4,-4,-4]], dtype=cp.float32)

# Maximum detail recovery kernel with stronger edge emphasis
highfreq_kernel = cp.array([[-2,-3,-2],
                          [-3, 25,-3],
                          [-2,-3,-2]], dtype=cp.float32)

# Enhanced edge detection with multi-directional sensitivity
edge_kernel = cp.array([[-3,-3,-3,-3,-3],
                       [-3, 4, 4, 4,-3],
                       [-3, 4, 16, 4,-3],
                       [-3, 4, 4, 4,-3],
                       [-3,-3,-3,-3,-3]], dtype=cp.float32)

# Fine detail enhancement kernel for microscopic features
detail_kernel = cp.array([[-1,-2,-1],
                         [-2, 13,-2],
                         [-1,-2,-1]], dtype=cp.float32)

class FocusStacker:
    def __init__(self, radius=8, smoothing=4, scale_factor=2):
        """
        @param radius: Size of the focus measure window (1-20)
        @param smoothing: Amount of smoothing applied to focus maps (1-10)
        @param scale_factor: Processing scale multiplier (1-4). Higher values may improve detail but increase processing time.
                           1 = original resolution
                           2 = 2x upscaling (default, recommended)
                           3 = 3x upscaling (more detail, slower)
                           4 = 4x upscaling (maximum detail, much slower)
        """
        if not 1 <= radius <= 20:
            raise ValueError("Radius must be between 1 and 20")
        if not 1 <= smoothing <= 10:
            raise ValueError("Smoothing must be between 1 and 10")
        if not 1 <= scale_factor <= 4:
            raise ValueError("Scale factor must be between 1 and 4")
            
        self.radius = radius
        self.smoothing = smoothing
        self.scale_factor = scale_factor
        self.window_size = 2 * radius + 1
        self._init_color_profiles()

    def _init_color_profiles(self):
        self.color_profiles = {
            'sRGB': PIL.ImageCms.createProfile('sRGB')
        }

    def _load_image(self, path):
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError(f"Failed to load image: {path}")
        return cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255

    def _align_images(self, images):
        print("\nAligning images using GPU...")
        reference = images[0]
        aligned = [reference]
        
        ref_gray = cv2.cvtColor((reference * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
        gpu_ref = cp.asarray(ref_gray.astype(np.float32))
        
        # Convert to GPU and normalize
        gpu_ref = cp.asarray(ref_gray.astype(np.float32))
        gpu_ref = (gpu_ref - cp.min(gpu_ref)) / (cp.max(gpu_ref) - cp.min(gpu_ref))
        
        for i, img in enumerate(images[1:], 1):
            print(f"Aligning image {i+1} with reference...")
            
            try:
                img_gray = cv2.cvtColor((img * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
                img_gray = cv2.normalize(img_gray, None, 0, 255, cv2.NORM_MINMAX)
                
                # Enhanced multi-scale alignment with finer scale steps
                scales = [1.0, 0.8, 0.6, 0.4, 0.2]  # More granular scale steps
                best_shift = None
                best_error = float('inf')
                
                for scale in scales:
                    if scale != 1.0:
                        width = int(img_gray.shape[1] * scale)
                        height = int(img_gray.shape[0] * scale)
                        scaled_ref = cv2.resize(ref_gray, (width, height))
                        scaled_img = cv2.resize(img_gray, (width, height))
                    else:
                        scaled_ref = ref_gray
                        scaled_img = img_gray
                    
                    # Convert to GPU arrays first
                    gpu_scaled_ref = cp.asarray(scaled_ref.astype(np.float32))
                    gpu_scaled_img = cp.asarray(scaled_img.astype(np.float32))
                    
                    # Apply contrast enhancement directly on GPU
                    gpu_scaled_ref = (gpu_scaled_ref - cp.min(gpu_scaled_ref)) / (cp.max(gpu_scaled_ref) - cp.min(gpu_scaled_ref))
                    gpu_scaled_img = (gpu_scaled_img - cp.min(gpu_scaled_img)) / (cp.max(gpu_scaled_img) - cp.min(gpu_scaled_img))
                    
                    # Enhanced phase correlation with higher upsampling
                    shift, error, _ = phase_cross_correlation(
                        gpu_scaled_ref.get(),
                        gpu_scaled_img.get(),
                        upsample_factor=20  # Increased precision
                    )
                    
                    if scale != 1.0:
                        shift = shift / scale
                    
                    shifted_img = cv2.warpAffine(
                        img_gray, 
                        np.float32([[1, 0, -shift[1]], [0, 1, -shift[0]]]),
                        (img_gray.shape[1], img_gray.shape[0]),
                        flags=cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_REFLECT
                    )
                    
                    error = -cv2.matchTemplate(
                        ref_gray, 
                        shifted_img, 
                        cv2.TM_CCOEFF_NORMED
                    )[0][0]
                    
                    if error < best_error:
                        best_error = error
                        best_shift = shift
                
                shift = best_shift
                error = best_error
                print(f"Detected shift: {shift}, error: {error}")
                
                M = np.float32([[1, 0, -shift[1]], [0, 1, -shift[0]]])
                aligned_img = cv2.warpAffine(
                    img, M, (img.shape[1], img.shape[0]),
                    flags=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_REFLECT
                )
                
                if error > 0.1:
                    try:
                        warp_matrix = np.eye(2, 3, dtype=np.float32)
                        criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 1000, 1e-7)
                        _, warp_matrix = cv2.findTransformECC(
                            ref_gray,
                            cv2.cvtColor((aligned_img * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY),
                            warp_matrix, cv2.MOTION_EUCLIDEAN, criteria
                        )
                        aligned_img = cv2.warpAffine(
                            aligned_img, warp_matrix, (img.shape[1], img.shape[0]),
                            flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_REFLECT
                        )
                        print("Applied ECC refinement")
                    except Exception as e:
                        print(f"Warning: ECC refinement failed: {str(e)}")
                
                aligned.append(aligned_img)
                print(f"Successfully aligned image {i+1}")
                
            except Exception as e:
                print(f"Error aligning image {i+1}: {str(e)}")
                print("Using original image as fallback")
                aligned.append(img)
        
        return aligned

    def _focus_measure(self, img):
        """
        Optimized focus measure calculation using parallel GPU operations
        """
        if len(img.shape) == 3:
            img = cv2.cvtColor((img * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
        else:
            img = (img * 255).astype(np.uint8)
            
        # Convert to GPU array once
        gpu_img = cp.asarray(img.astype(np.float32))
        
        # Pre-calculate common image derivatives for all scales
        dx = cp.asarray(cv2.Sobel(cp.asnumpy(gpu_img), cv2.CV_32F, 1, 0, ksize=3))
        dy = cp.asarray(cv2.Sobel(cp.asnumpy(gpu_img), cv2.CV_32F, 0, 1, ksize=3))
        gradient_magnitude = cp.sqrt(dx*dx + dy*dy)
        
        # Pre-calculate Laplacian for edge detection
        laplacian = cp.asarray(cv2.Laplacian(cp.asnumpy(gpu_img), cv2.CV_32F))
        
        # Parallel multi-scale analysis
        scales = [1.0, 0.5, 0.25]
        weights = [0.6, 0.3, 0.1]
        
        focus_map = cp.zeros_like(gpu_img)
        
        for scale, weight in zip(scales, weights):
            if scale != 1.0:
                scaled = cp.asarray(cv2.resize(cp.asnumpy(gpu_img), None, fx=scale, fy=scale))
                scaled_grad = cp.asarray(cv2.resize(cp.asnumpy(gradient_magnitude), None, fx=scale, fy=scale))
                scaled_lap = cp.asarray(cv2.resize(cp.asnumpy(laplacian), None, fx=scale, fy=scale))
            else:
                scaled = gpu_img
                scaled_grad = gradient_magnitude
                scaled_lap = laplacian
            
            # Parallel frequency analysis
            high_freq = cp.abs(scaled - cp.asarray(cv2.GaussianBlur(cp.asnumpy(scaled), (5,5), 0)))
            
            # Parallel edge detection
            edge_strength = cp.abs(scaled_lap)
            
            # Local contrast in parallel
            local_mean = cp.asarray(cv2.GaussianBlur(cp.asnumpy(scaled), (7,7), 1.5))
            local_contrast = cp.abs(scaled - local_mean)
            
            # Combine measures
            scale_measure = high_freq * edge_strength * local_contrast * scaled_grad
            
            # Resize back to original size if needed
            if scale != 1.0:
                scale_measure = cp.asarray(cv2.resize(cp.asnumpy(scale_measure), (gpu_img.shape[1], gpu_img.shape[0])))
            
            focus_map += weight * scale_measure
            
            # Clear intermediate results
            del high_freq, edge_strength, local_mean, local_contrast, scale_measure
            if scale != 1.0:
                del scaled, scaled_grad, scaled_lap
        
        # Final enhancement
        focus_map = (focus_map - cp.min(focus_map)) / (cp.max(focus_map) - cp.min(focus_map) + 1e-6)
        
        # Edge-aware enhancement
        edge_mask = cp.clip(cp.abs(laplacian) / (cp.max(cp.abs(laplacian)) + 1e-6), 0, 1)
        focus_map = focus_map * (1.0 + 0.2 * edge_mask)
        
        # Normalize and cleanup
        focus_map = cp.clip((focus_map - cp.min(focus_map)) / (cp.max(focus_map) - cp.min(focus_map) + 1e-6), 0, 1)
        
        result = cp.asnumpy(focus_map).astype(np.float32)
        
        # Clear GPU memory
        del gpu_img, dx, dy, gradient_magnitude, laplacian, focus_map, edge_mask
        cp.get_default_memory_pool().free_all_blocks()
        
        return result

    def _blend_images(self, aligned_images, focus_maps):
        """
        Enhanced blending with depth-aware processing and GPU optimization
        """
        h, w = aligned_images[0].shape[:2]
        if self.scale_factor > 1:
            new_h, new_w = h * self.scale_factor, w * self.scale_factor
        else:
            new_h, new_w = h, w
            
        # Initialize result arrays on GPU
        result = cp.zeros((new_h, new_w, 3), dtype=cp.float32)
        weights_sum = cp.zeros((new_h, new_w, 1), dtype=cp.float32)
        
        # Pre-process focus maps to match dimensions
        resized_focus_maps = []
        for fm in focus_maps:
            if fm.shape[:2] != (h, w):
                fm = cv2.resize(fm, (w, h), interpolation=cv2.INTER_LINEAR)
            resized_focus_maps.append(fm)
        
        # Process each image with optimized GPU operations
        for img, fm in zip(aligned_images, resized_focus_maps):
            # Clear GPU cache before processing each image
            cp.get_default_memory_pool().free_all_blocks()
            
            # Scale image and focus map
            img_up = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
            fm_up = cv2.resize(fm, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            
            # Convert to GPU arrays
            gpu_img = cp.asarray(img_up)
            gpu_fm = cp.asarray(fm_up)
            if len(gpu_fm.shape) == 2:
                gpu_fm = gpu_fm.reshape(gpu_fm.shape[0], gpu_fm.shape[1], 1)
            fm_2d = gpu_fm[..., 0] if len(gpu_fm.shape) > 2 else gpu_fm
            
            # Calculate depth gradients on GPU
            dx = cp.asarray(cv2.Sobel(cp.asnumpy(fm_2d), cv2.CV_32F, 1, 0, ksize=3))
            dy = cp.asarray(cv2.Sobel(cp.asnumpy(fm_2d), cv2.CV_32F, 0, 1, ksize=3))
            depth_gradient = cp.sqrt(dx*dx + dy*dy)
            del dx, dy
            
            # Create depth-aware mask (keep bilateral filter for quality)
            depth_mask = cp.asarray(cv2.bilateralFilter(cp.asnumpy(depth_gradient), 9, 75, 75))
            depth_mask = (depth_mask - cp.min(depth_mask)) / (cp.max(depth_mask) - cp.min(depth_mask) + 1e-6)
            del depth_gradient
            
            # Multi-scale analysis with GPU memory optimization
            fm_new = cp.zeros_like(fm_2d)
            scales = [200, 150, 100, 50]  # Keep original scales for quality
            weights = [0.35, 0.3, 0.2, 0.15]
            
            for scale, weight in zip(scales, weights):
                # Process each scale with minimal GPU-CPU transfers
                kernel_size = (scale*2+1, scale*2+1)
                sigma = scale/3
                
                # Single GPU transfer for Gaussian blur
                fm_blur = cp.asarray(cv2.GaussianBlur(cp.asnumpy(fm_2d), kernel_size, sigma))
                
                # Compute edge strength on GPU
                edge_strength = cp.abs(fm_2d - fm_blur)
                edge_strength *= (1.0 + depth_mask)  # Depth-aware edge boost
                
                # Local statistics on GPU
                edge_sq = edge_strength * edge_strength
                local_std = cp.asarray(cv2.GaussianBlur(cp.asnumpy(edge_sq), (25, 25), 0))
                threshold = cp.mean(edge_strength) + cp.std(edge_strength) * (2.0 + depth_mask)
                
                # Combine with depth-aware weighting
                blend_weight = weight * (1.0 + 0.5 * depth_mask)
                fm_new += cp.where(edge_strength > threshold,
                                 fm_blur * blend_weight,
                                 fm_2d * blend_weight)
                
                # Clean up scale-specific arrays
                del fm_blur, edge_strength, edge_sq, local_std
            
            # Bilateral filtering with minimal transfers
            smoothed = cp.asarray(cv2.bilateralFilter(cp.asnumpy(fm_new), 11, 100, 100))
            smoothed = cp.asarray(cv2.bilateralFilter(cp.asnumpy(smoothed), 7, 50, 50))
            
            # Normalize and prepare weight
            weight = (smoothed - cp.min(smoothed)) / (cp.max(smoothed) - cp.min(smoothed) + 1e-6)
            weight = weight.reshape(weight.shape[0], weight.shape[1], 1)
            
            # Blend on GPU
            result += gpu_img * weight
            weights_sum += weight
            
            # Clean up image-specific arrays
            del gpu_img, gpu_fm, fm_2d, fm_new, smoothed, weight, depth_mask
            cp.get_default_memory_pool().free_all_blocks()
            
        # Normalize result
        result = result / (weights_sum + 1e-10)
        
        # Calculate input statistics and preserve original brightness characteristics
        ref_img = cp.asarray(aligned_images[0])
        input_mean = float(cp.mean(ref_img))
        input_std = float(cp.std(ref_img))
        max_ref = float(cp.max(ref_img))
        
        # Simpler dynamic range preservation that maintains original brightness
        # Calculate reference statistics
        ref_min = float(cp.min(ref_img))
        ref_max = float(cp.max(ref_img))
        ref_range = ref_max - ref_min
        
        # Normalize result to match reference range
        result_min = float(cp.min(result))
        result_max = float(cp.max(result))
        result = (result - result_min) * (ref_range / (result_max - result_min + 1e-6)) + ref_min
        
        # Apply gentle contrast enhancement
        for c in range(3):
            # Calculate channel-specific reference stats
            channel_min = float(cp.min(ref_img[...,c]))
            channel_max = float(cp.max(ref_img[...,c]))
            channel_mean = float(cp.mean(ref_img[...,c]))
            
            # Preserve original range while gently enhancing contrast
            result[...,c] = cp.clip(result[...,c], channel_min, channel_max)
            # Adjust contrast while maintaining mean
            result[...,c] = (result[...,c] - channel_mean) * 1.1 + channel_mean
            
        # Final range adjustment
        result = cp.clip(result, 0.0, max_ref)
        
        # Clean up reference image
        del ref_img
        
        # Debug sharpening mask dimensions
        print(f"\nSharpening mask calculation:")
        print(f"Result shape: {result.shape}")
        print(f"Focus mask initial shape: {result[...,0].shape}")
        
        # Compute focus-aware sharpening mask using resized focus maps
        focus_mask = cp.zeros_like(result[...,0])
        for i, fm in enumerate(resized_focus_maps):
            print(f"Processing focus map {i+1}:")
            print(f"Original shape: {fm.shape}")
            # Resize focus map to match processing dimensions
            fm_up = cv2.resize(fm, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            print(f"Upscaled shape: {fm_up.shape}")
            gpu_fm = cp.asarray(fm_up)
            if len(gpu_fm.shape) > 2:
                gpu_fm = gpu_fm[..., 0]
            print(f"GPU array shape: {gpu_fm.shape}")
            focus_mask += (1.0 - gpu_fm)
            del gpu_fm, fm_up
            
        print(f"Final focus mask shape: {focus_mask.shape}")
        focus_mask /= len(focus_maps)
        focus_mask = cp.clip(focus_mask, 0.3, 1.0)
        
        # Apply sharpening to full image at once
        sharp_result = cp.zeros_like(result)
        for c in range(3):
            # Basic sharpening
            sharp = cp.real(cp.fft.ifft2(
                cp.fft.fft2(result[...,c]) * cp.fft.fft2(sharp_kernel, s=result[...,c].shape)
            ))
            
            # High-frequency enhancement
            high_freq = cp.real(cp.fft.ifft2(
                cp.fft.fft2(result[...,c]) * cp.fft.fft2(highfreq_kernel, s=result[...,c].shape)
            ))
            
            # Enhanced multi-scale sharpening with extreme detail preservation
            # Calculate local variance with finer sensitivity
            local_var = cp.asarray(cv2.GaussianBlur(cp.asnumpy(result[...,c] * result[...,c]), (11, 11), 0)) - \
                       cp.power(cp.asarray(cv2.GaussianBlur(cp.asnumpy(result[...,c]), (11, 11), 0)), 2)
            
            # Enhanced detail mask with stronger edge detection
            detail_mask = cp.clip((local_var - cp.min(local_var)) / (cp.max(local_var) - cp.min(local_var) + 1e-6), 0.4, 1.0)
            
            # Fine detail enhancement
            fine_detail = cp.real(cp.fft.ifft2(
                cp.fft.fft2(result[...,c]) * cp.fft.fft2(detail_kernel, s=result[...,c].shape)
            ))
            
            # Adaptive multi-scale sharpening
            sharp_strength = cp.clip(focus_mask * (1.4 + 0.4 * detail_mask), 0.8, 0.99)
            
            # Gentler detail enhancement that preserves original brightness
            # Calculate local contrast for adaptive sharpening
            local_contrast = cp.asarray(cv2.Laplacian(cp.asnumpy(result[...,c]), cv2.CV_32F))
            contrast_mask = cp.clip(cp.abs(local_contrast) / (cp.max(cp.abs(local_contrast)) + 1e-6), 0.2, 0.6)
            
            # Combine enhancements with reduced strength
            sharp_result[...,c] = result[...,c] + \
                                 sharp * sharp_strength * 0.5 * contrast_mask + \
                                 high_freq * 0.2 * contrast_mask + \
                                 fine_detail * 0.1 * contrast_mask  # Minimal enhancement to preserve brightness
            
            # Clear intermediate results
            del sharp, high_freq
            cp.get_default_memory_pool().free_all_blocks()
        
        result = sharp_result
        del sharp_result
        
        # Convert back to CPU and downscale if needed
        if self.scale_factor > 1:
            result_np = cv2.resize(cp.asnumpy(result), (w, h), 
                                 interpolation=cv2.INTER_LANCZOS4)
        else:
            result_np = cp.asnumpy(result)
            
        # Final normalization and clipping
        result_np = np.clip(result_np, 0, 1)
        
        # Clear GPU memory
        del result, focus_mask
        cp.get_default_memory_pool().free_all_blocks()
        
        return result_np

    def split_into_stacks(self, image_paths, stack_size):
        import re
        
        stacks_dict = {}
        for path in image_paths:
            filename = os.path.basename(path)
            name, ext = os.path.splitext(filename)
            
            patterns = [
                r'^(.*?)[-_]?(\d+)$',
                r'^(.*?)[-_]?(\d+)[-_]',
                r'(\d+)[-_]?(.*?)$'
            ]
            
            matched = False
            for pattern in patterns:
                match = re.match(pattern, name)
                if match:
                    groups = match.groups()
                    base_name = groups[0] if len(groups[0]) > len(groups[1]) else groups[1]
                    if base_name not in stacks_dict:
                        stacks_dict[base_name] = []
                    stacks_dict[base_name].append(path)
                    matched = True
                    break
                    
            if not matched:
                print(f"Warning: Could not match pattern for file: {filename}")
                base_name = name
                if base_name not in stacks_dict:
                    stacks_dict[base_name] = []
                stacks_dict[base_name].append(path)
            
        for base_name in stacks_dict:
            stacks_dict[base_name].sort()
            
        stacks = list(stacks_dict.values())
        
        expected_size = stack_size
        for i, stack in enumerate(stacks):
            if len(stack) != expected_size:
                print(f"Warning: Stack {i+1} has {len(stack)} images, expected {expected_size}")
                print(f"Stack contents: {[os.path.basename(p) for p in stack]}")
                
        stacks.sort(key=lambda x: x[0])
        
        print("\nDetected stacks:")
        for i, stack in enumerate(stacks):
            print(f"Stack {i+1}: {[os.path.basename(p) for p in stack]}")
            
        return stacks

    def process_stack(self, image_paths, color_space='sRGB'):
        if len(image_paths) < 2:
            raise ValueError("At least 2 images are required")
            
        print(f"\nProcessing stack of {len(image_paths)} images...")
        print("Image paths:", image_paths)
            
        images = []
        for i, path in enumerate(image_paths):
            print(f"Loading image {i+1}/{len(image_paths)}: {path}")
            try:
                img = self._load_image(path)
                images.append(img)
                print(f"Successfully loaded image {i+1} with shape {img.shape}")
            except Exception as e:
                print(f"Error loading image {path}: {str(e)}")
                raise
                
        print("\nAligning images...")
        try:
            aligned = self._align_images(images)
            print(f"Successfully aligned {len(aligned)} images")
        except Exception as e:
            print(f"Error during image alignment: {str(e)}")
            raise
            
        print("\nCalculating focus measures...")
        focus_maps = []
        for i, img in enumerate(aligned):
            print(f"Computing focus measure for image {i+1}/{len(aligned)}")
            try:
                focus_map = self._focus_measure(img)
                focus_maps.append(focus_map)
                print(f"Focus measure computed for image {i+1}")
            except Exception as e:
                print(f"Error calculating focus measure for image {i+1}: {str(e)}")
                raise
                
        print("\nBlending images...")
        try:
            result = self._blend_images(aligned, focus_maps)
            print("Successfully blended images")
        except Exception as e:
            print(f"Error during image blending: {str(e)}")
            raise
            
        if color_space != 'sRGB':
            print(f"\nConverting to {color_space} color space...")
            try:
                result = self._convert_color_space(result, color_space)
                print("Color space conversion complete")
            except Exception as e:
                print(f"Error during color space conversion: {str(e)}")
                raise
            
        print("\nStack processing complete!")
        return result

    def _convert_color_space(self, img, target_space):
        pil_img = PIL.Image.fromarray((img * 255).astype('uint8'))
        
        source_profile = self.color_profiles['sRGB']
        target_profile = self.color_profiles[target_space]
        transform = PIL.ImageCms.buildTransformFromOpenProfiles(
            source_profile, target_profile, "RGB", "RGB")
        
        converted = PIL.ImageCms.applyTransform(pil_img, transform)
        
        return np.array(converted).astype(np.float32) / 255
    def save_image(self, img, path, format='JPEG', color_space='sRGB'):
        """
        Save the processed image to a file
        @param img: The image array to save
        @param path: Output file path
        @param format: Image format (JPEG)
        @param color_space: Color space to use (sRGB)
        """
        print(f"\nSaving image as {format}...")
        print(f"Path: {path}")
        
        try:
            # Convert to 8-bit with proper rounding
            img_8bit = np.clip(img * 255.0 + 0.5, 0, 255).astype(np.uint8)
            
            # Create PIL image
            pil_img = PIL.Image.fromarray(img_8bit, mode='RGB')
            
            # Save with format-specific settings
            if format.upper() == 'JPEG':
                pil_img.save(path, format='JPEG', quality=95, optimize=True)
            else:
                raise ValueError(f"Unsupported format: {format}")
                
            print(f"Successfully saved image to {path}")
            
        except Exception as e:
            print(f"Error saving image: {str(e)}")
            raise
